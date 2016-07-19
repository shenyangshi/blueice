import pickle
import collections

import numpy as np
import matplotlib.pyplot as plt
from tqdm import tqdm
from copy import deepcopy

from multihist import Histdd
from .source import Source
from . import utils

# Features I would like to add:
#  - Non-asymptotic limit setting
#  - General (shape) uncertainties
#  - Fit parameters other than source strength

class Model(object):
    """Model for XENON1T dataset simulation and analysis

    Dormant sources have their pdf computed on initialization, but are not considered in the likelihood computation.
    Use activate_source(source_id) and deactivate_source(source_id) to swap sources between dormant and active.

    Setting analysis_target = True ensures that:
     - Only one source with analysis_target = True can be active at the same time.
       If you activate a new one, the old one will be deactivated.
     - The source is always last in the model.sources list. This is used in the analysis methods.
    """
    config = None            # type: dict
    no_wimp_strength = -10
    max_wimp_strength = 4
    exposure_factor = 1      # Increase this to quickly change the exposure of the model

    def __init__(self, config, ipp_client=None, **kwargs):
        """
        :param config: Dictionary specifying detector parameters, source info, etc.
        :param ipp_client: ipyparallel client to use for parallelizing pdf computation (optional)
        :param kwargs: Overrides for the config (optional)
        :return:
        """
        self.config = deepcopy(config)
        self.config.update(kwargs)

        self.space = collections.OrderedDict(self.config['analysis_space'])
        self.dims = list(self.space.keys())
        self.bins = list(self.space.values())

        with open(utils.data_file_name(self.config['s1_relative_ly_map']), mode='rb') as infile:
            self.config['s1_relative_ly_map'] = pickle.load(infile)

        self.sources = self._init_sources(self.config['sources'], ipp_client=ipp_client)
        self.dormant_sources = self._init_sources(self.config['dormant_sources'], ipp_client=ipp_client)

    def _init_sources(self, source_specs, ipp_client=None):
        result = []
        for source_spec in source_specs:
            source = Source(self.config, source_spec)

            # Has the PDF in the analysis space already been provided in the spec?
            # Usually not: do so now.
            if source.pdf_histogram is None or c['force_pdf_recalculation']:
                self.compute_source_pdf(source, ipp_client=ipp_client)
            result.append(source)
        return result

    def compute_source_pdf(self, source, ipp_client=None):
        """Computes the PDF of the source in the analysis space.
        Returns nothing, modifies source in-place.
        :param ipp_client: ipyparallel client to use for parallelizing pdf computation (optional)
        """
        # Not a method of source, since it needs analysis space definition...
        # To be honest I'm not sure where this method (and source.simulate) actually would fit best.

        # Simulate batches of events at a time (to avoid memory errors, show a progressbar, and split up among machines)
        # Number of events to simulate will be rounded up to the nearest batch size
        batch_size = self.config['pdf_sampling_batch_size']
        n_batches = int((source.n_events_for_pdf * self.config['pdf_sampling_multiplier']) // batch_size + 1)
        n_events = n_batches * batch_size
        mh = Histdd(bins=self.bins)

        if ipp_client is not None:
            # We need both a directview and a load-balanced view: the latter doesn't have methods like push.
            directview = ipp_client[:]
            lbview = ipp_client.load_balanced_view()

            # Get the necessary objects to the engines
            # For some reason you can't directly .push(dict(bins=self.bins)),
            # it will fail with 'bins' is not defined error. When you first assign bins = self.bins it works.
            bins = self.bins
            dims = self.dims

            def to_space(d):
                """Standalone counterpart of Model.to_space, needed for parallel simulation. Ugly!!"""
                return [d[dims[i]] for i in range(len(dims))]

            def do_sim(_):
                """Run one simulation batch and histogram it immediately (so we don't pass gobs of data around)"""
                return Histdd(*to_space(source.simulate(batch_size)), bins=bins).histogram

            directview.push(dict(source=source, bins=bins, dims=dims, batch_size=batch_size, to_space=to_space),
                                  block=True)

            amap_result = lbview.map(do_sim, [None for _ in range(n_batches)], ordered=False,
                                     block=self.config.get('block_during_simulation', False))
            for r in tqdm(amap_result, total=n_batches, desc='Sampling PDF of %s' % source.name):
                mh.histogram += r

        else:
            for _ in tqdm(range(n_batches),
                          desc='Sampling PDF of %s' % source.name):
                mh.add(*self.to_space(source.simulate(batch_size)))

        source.fraction_in_range = mh.n / n_events

        # Convert the histogram to a PDF
        # This means we have to divide by
        #  - the number of events histogrammed
        #  - the bin sizes (particularly relevant for non-uniform bins!)
        source.pdf_histogram = mh.similar_blank_hist()
        source.pdf_histogram.histogram = mh.histogram.astype(np.float) / mh.n
        source.pdf_histogram.histogram /= np.outer(*[np.diff(self.bins[i]) for i in range(len(self.bins))])

        # Estimate the MC statistical error. Not used for anything, but good to inspect.
        source.pdf_errors = source.pdf_histogram / np.sqrt(np.clip(mh.histogram, 1, float('inf')))
        source.pdf_errors[source.pdf_errors == 0] = float('nan')

    def get_source_i(self, source_id, from_dormant=False):
        if isinstance(source_id, (int, float)):
            return int(source_id)
        else:
            for s_i, s in enumerate(self.sources if not from_dormant else self.dormant_sources):
                if source_id in s.name:
                    break
            else:
                raise ValueError("Unknown source %s" % source_id)
            return s_i

    def activate_source(self, source_id):
        """Activates the source named source_id from the list of dormant sources."""
        # Is the source already active?
        if source_id in [s.name for s in model.sources]:
            print("Source %s is already active - nothing done." % source_id)
            return
        dormant_source_i = self.get_source_i(source_id, from_dormant=True)
        s = self.dormant_sources[dormant_source_i]
        if s.analysis_target:
            # Insert the new analysis target at the end of the list
            self.deactivate_source(-1, force=True)
            self.sources.append(s)
        else:
            self.sources = self.sources[:-1] + [s] + self.sources[-1]
        del self.dormant_sources[dormant_source_i]

    def deactivate_source(self, source_id, force=False):
        """Deactivates the source named source_id"""
        source_i = self.get_source_i(source_id)
        s = self.sources[source_i]
        if not force and s.analysis_target:
            raise ValueError("Cannot deactivate the analysis target: please activate a new one instead.")
        self.dormant_sources.append(s)
        del self.sources[source_i]

    def range_cut(self, d, ps=None):
        """Return events from dataset d which are in the analysis space
        Also removes events for which all of the source PDF's are zero (which cannot be meaningfully interpreted)
        """
        mask = np.ones(len(d), dtype=np.bool)
        for dimension, bin_edges in self.space.items():
            mask = mask & (d[dimension] >= bin_edges[0]) & (d[dimension] <= bin_edges[-1])

        # Ignore events to which no source pdf assigns a positive probability.
        # These would cause log(sum_sources (mu * p)) in the loglikelihood to become -inf.
        if ps is None:
            ps = self.score_events(d)
        mask &= ps.sum(axis=0) != 0

        return d[mask]

    def simulate(self, wimp_strength=0, restrict=True):
        """Makes a toy dataset.
        if restrict=True, return only events inside analysis range
        """
        ds = []
        for s_i, source in enumerate(self.sources):
            n = np.random.poisson(source.events_per_day *
                                  self.config['livetime_days'] *
                                  (10**wimp_strength if source.name.startswith('wimp') else 1) *
                                  self.exposure_factor)
            d = source.simulate(n)
            d['source'] = s_i
            ds.append(d)
        d = np.concatenate(ds)
        if restrict:
            d = self.range_cut(d)
        return d

    def show(self, d, ax=None, dims=(0, 1)):
        """Plot the events from dataset d in the analysis range
        ax: plot on this Axes
        Dims: tuple of numbers indicating which two dimensions to plot in.
        """
        if ax is None:
            ax = plt.gca()

        d = self.range_cut(d)
        for s_i, s in enumerate(self.sources):
            q = d[d['source'] == s_i]
            q_in_space = self.to_space(q)
            ax.scatter(q_in_space[dims[0]],
                       q_in_space[dims[1]],
                       color=s.color, s=5, label=s.label)

        ax.set_xlabel(self.dims[dims[0]])
        ax.set_ylabel(self.dims[dims[1]])
        ax.set_xlim(self.bins[dims[0]][0], self.bins[dims[0]][-1])
        ax.set_ylim(self.bins[dims[1]][0], self.bins[dims[1]][-1])

    def to_space(self, d):
        """Given a dataset, returns list of arrays of coordinates of the events in the analysis dimensions"""
        return [d[self.dims[i]] for i in range(len(self.dims))]

    def score_events(self, d):
        """Returns array (n_sources, n_events) of pdf values for each source for each of the events"""
        return np.vstack([s.pdf(*self.to_space(d)) for s in self.sources])

    def expected_events(self, s=None):
        """Return the total number of events expected in the analysis range for the source s.
        If no source specified, return an array of results for all sources.
        """
        if s is None:
            return np.array([self.expected_events(s) for s in self.sources])
        return s.events_per_day * self.config['livetime_days'] * s.fraction_in_range * self.exposure_factor

    def loglikelihood(self, d, mu=None, ps=None):
        """Return the log-likelihood of the dataset d under the model,
        mu: array of n_sources, expected number of events for each source.
        """
        # TODO: this function should no longer exist!
        if ps is None:
            ps = self.score_events(d)
        if mu is None:
            mu = np.array([self.expected_events(s) for s in m.sources])

        # Return the extended log likelihood (without tedious normalization constant that anyway drops out of
        # likelihood ratio computations).
        return utils.extended_maximum_likelihood(mu, ps)
        return result

    # Utility methods
    @staticmethod
    def load(filename):
        with open(filename, mode='rb') as infile:
            return pickle.load(infile)

    def save(self, filename=None):
        if filename is None:
            filename = 'model_' + str(np.random.random())
        with open(filename, mode='wb') as outfile:
            pickle.dump(self, outfile)
        return filename

    def copy(self):
        return deepcopy(self)
