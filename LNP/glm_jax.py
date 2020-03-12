# -*- coding: utf-8 -*-

from functools import partial
from importlib import import_module
from typing import Dict, Tuple

import jax.numpy as np
import numpy as onp
from jax import devices, jit, random, grad, value_and_grad
from jax.config import config
from jax.experimental.optimizers import OptimizerState
from jax.interpreters.xla import DeviceArray

try:  # kernprof
    profile
except NameError:
    def profile(x): return x


class GLMJax:
    def __init__(self, p: Dict, theta=None, optimizer=None, use_gpu=False, offline_data=None):
        """
        A JAX implementation of simGLM.

        Assuming that all parameters are fixed except N and M.
        - N can be increased forever (at a cost).
        - M can only be increased to M_lim.

        There is a substantial cost to increasing N. It is a better idea to keep N reasonably high at initialization.

        :param p: Dictionary of parameters. λ is L1 sparsity.
        :type p: dict
        :param theta: Dictionary of ndarray weights. Must conform to parameters in p.
        :type theta: dict
        :param optimizer: Dictionary of optimizer name and hyperparameters from jax.experimental.optimizers. Ex. {'name': 'SGD, **kwargs}
        :type optimizer: dict
        :param use_gpu: Use GPU
        :type use_gpu: bool
        :param offline_data: (y, s) full offline data (to prevent unnecessary CPU/GPU transfers)
        """

        self.use_gpu = use_gpu
        platform = 'gpu' if use_gpu else 'cpu'  # Restart interpreter when switching platform.
        config.update('jax_platform_name', platform)
        print('Using', devices()[0])

        if not self.use_gpu:  # JAX defaults to single precision.
            config.update("jax_enable_x64", True)

        # p check
        if not all(k in p for k in ['ds', 'dh', 'dt', 'N_lim', 'M_lim']):
            raise ValueError('Parameter incomplete!')
        self.params = p

        # θ check
        if theta is None:
            raise ValueError('θ not specified.')
        else:
            assert theta['w'].shape == (p['N_lim'], p['N_lim'])
            assert theta['h'].shape == (p['N_lim'], p['dh'])
            assert theta['k'].shape == (p['N_lim'], p['ds'])
            assert (theta['b'].shape == (p['N_lim'],)) or (theta['b'].shape == (p['N_lim'], 1))

            if len(theta['b'].shape) == 1:  # Array needs to be 2D.
                theta['b'] = np.reshape(theta['b'], (p['N_lim'], 1))

            self._θ = {k: np.asarray(v) for k, v in theta.items()}  # Transfer to device.

        # Optimizer
        if optimizer is None:
            raise ValueError('Optimizer not named.')  # = {'name': 'sgd', 'step_size': 1e-5}
        print(f'Optimizer: {optimizer}')
        opt_func = getattr(import_module('jax.experimental.optimizers'), optimizer['name'])
        optimizer = {k: v for k, v in optimizer.items() if k != 'name'}
        self.opt_init, self.opt_update, self.get_params = opt_func(**optimizer)
        self._θ: OptimizerState = self.opt_init(self._θ)

        # Optimization for offline training. Reduce CPU/GPU data transfer.
        self.offline = True if offline_data else False
        if self.offline:
            self.y, self.s = [np.asarray(data) for data in offline_data]
            self.rand = np.zeros(0)

        self.current_N = 0
        self.current_M = 0
        self.iter = 0

    def ll(self, y, s) -> float:
        args = self._check_arrays(y, s)
        return float(self._ll(self.θ, self.params, *args))

    @profile
    def fit(self, y, s, return_ll=False):
        """
        If offline, (y, s) is not used.
        """
        if self.iter % 10000 == 0:
            # Batch training.
            self.rand = onp.random.randint(low=0, high=self.y.shape[1] - self.params['M_lim'] + 1, size=10000)

        if self.offline:
            i = self.rand[self.iter % 10000]
            args = (self.params['N_lim'] * self.params['M_lim'],
                    self.y[:, i:i+self.params['M_lim']],
                    self.s[:, i:i+self.params['M_lim']])
        else:
            args = self._check_arrays(y, s)

        if return_ll:
            ll, Δ = value_and_grad(self._ll)(self.θ, self.params, *args)
        else:
            Δ = grad(self._ll)(self.θ, self.params, *args)

        self._θ: OptimizerState = jit(self.opt_update)(self.iter, Δ, self._θ)
        self.iter += 1

        return ll if return_ll else None

    def get_grad(self, y, s) -> DeviceArray:
        return grad(self._ll)(self.θ, self.params, *self._check_arrays(y, s))[1]

    def _check_arrays(self, y, s) -> Tuple[onp.ndarray]:
        """
        Check validity of input arrays and pad to N_lim and M_lim.
        :return padded arrays

        """
        assert y.shape[1] == s.shape[1]
        assert s.shape[0] == self.params['ds']

        self.current_N, self.current_M = y.shape

        N_lim = self.params['N_lim']
        M_lim = self.params['M_lim']

        while y.shape[0] > N_lim:
            self._increase_θ_size()
            N_lim = self.params['N_lim']

        # Pad
        if y.shape[0] < N_lim:
            y = onp.concatenate((y, onp.zeros((N_lim - y.shape[0], y.shape[1]))), axis=0)

        if y.shape[1] < M_lim:
            curr_size = y.shape[1]
            y = onp.concatenate((y, onp.zeros((N_lim, M_lim - curr_size))), axis=1)
            s = onp.concatenate((s, onp.zeros((self.params['ds'], M_lim - curr_size))), axis=1)

        if y.shape[1] > M_lim:
            raise ValueError('Data are too wide (M exceeds limit).')

        self.iter += 1
        return self.current_M * self.current_N, y, s

    def _increase_θ_size(self) -> None:
        """
        Doubles θ capacity for N in response to increasing N.

        """
        N_lim = self.params['N_lim']
        print(f"Increasing neuron limit to {2 * N_lim}.")
        self._θ = self.θ

        self._θ['w'] = np.concatenate((self._θ['w'], np.zeros((N_lim, N_lim))), axis=1)
        self._θ['w'] = np.concatenate((self._θ['w'], np.zeros((N_lim, 2 * N_lim))), axis=0)

        self._θ['h'] = np.concatenate((self._θ['h'], np.zeros((N_lim, self.params['dh']))), axis=0)
        self._θ['b'] = np.concatenate((self._θ['b'], np.zeros((N_lim, 1))), axis=0)
        self._θ['k'] = np.concatenate((self._θ['k'], np.zeros((N_lim, self.params['ds']))), axis=0)
        self.params['N_lim'] = 2 * N_lim
        self._θ = self.opt_init(self._θ)

    # Compiled Functions
    # A compiled function needs to know the shape of its inputs.
    # Changing array size will trigger a recompilation.
    # See https://jax.readthedocs.io/en/latest/notebooks/Common_Gotchas_in_JAX.html#%F0%9F%94%AA-Control-Flow

    @staticmethod
    @jit
    def log_zero(x):
        if x == 0.:
            return 0.
        else:
            return np.log(x)

    @staticmethod
    @partial(jit, static_argnums=(1,))
    def _ll(θ: Dict, p: Dict, curr_mn, y, s) -> DeviceArray:
        """
        Log-likelihood of a Poisson GLM.

        """
        N = p['N_lim']
        cal_stim = θ["k"][:N, :] @ s
        cal_hist = GLMJax._convolve(p, y, θ["h"][:N, :])
        cal_weight = θ["w"][:N, :N] @ y
        cal_weight = np.concatenate((np.zeros((N, p['dh'])), cal_weight[:, p['dh'] - 1:p['M_lim'] - 1]), axis=1)

        total = θ["b"][:N] + (cal_stim + cal_weight + cal_hist)

        r̂ = p['dt'] * np.exp(total)
        correction = p['dt'] * (np.size(y) - curr_mn)  # Remove padding

        l1 = p['λ1'] * np.mean(np.abs(θ["w"][:N, :N]))
        l2 = p['λ2'] * np.mean(θ["w"][:N, :N]**2)

        log_r̂ = np.log(r̂)
        log_r̂ *= np.isfinite(log_r̂)

        return (np.sum(r̂) - correction - np.sum(y * log_r̂)) / (N * curr_mn) + l1 + l2

    @staticmethod
    @partial(jit, static_argnums=(0,))
    def _convolve(p: Dict, y, θ_h) -> DeviceArray:
        """
        Sliding window convolution for θ['h'].
        """
        cvd = np.zeros((y.shape[0], y.shape[1] - p['dh']))
        for i in np.arange(p['dh']):
            w_col = np.reshape(θ_h[:, i], (p['N_lim'], 1))
            cvd += w_col * y[:, i:p['M_lim'] - (p['dh'] - i)]
        return np.concatenate((np.zeros((p['N_lim'], p['dh'])), cvd), axis=1)

    @property
    def θ(self) -> Dict:
        return self.get_params(self._θ)

    @property
    def weights(self) -> onp.ndarray:
        return onp.asarray(self.θ['w'][:self.current_N, :self.current_N])

    def __repr__(self):
        return f'simGLM({self.params}, θ={self.θ}, gpu={self.use_gpu})'

    def __str__(self):
        return f'simGLM Iteration: {self.iter}, \n Parameters: {self.params})'


if __name__ == '__main__':  # Test
    key = random.PRNGKey(42)
    from glm_py import GLMPy

    N = 2
    M = 100
    dh = 2
    ds = 8
    p = {'N': N, 'M': M, 'dh': dh, 'ds': ds, 'dt': 0.1, 'n': 0, 'N_lim': N, 'M_lim': M}

    w = random.normal(key, shape=(N, N)) * 0.001
    h = random.normal(key, shape=(N, dh)) * 0.001
    k = random.normal(key, shape=(N, ds)) * 0.001
    b = random.normal(key, shape=(N, 1)) * 0.001

    # w = np.zeros((N, N))
    # h = np.zeros((N, dh))
    # k = np.zeros((N, ds))
    # b = np.zeros((N, 1))

    theta = {'h': np.flip(h, axis=1), 'w': w, 'b': b, 'k': k}
    model = GLMJax(p, theta)

    sN = 8  #
    data = onp.random.randn(sN, 2)  # onp.zeros((8, 50))
    stim = onp.random.randn(ds, 2)
    print(model.ll(data, stim))


    def gen_ref():
        ow = onp.asarray(w)[:sN, :sN]
        oh = onp.asarray(h)[:sN, ...]
        ok = onp.asarray(k)[:sN, ...]
        ob = onp.asarray(b)[:sN, ...]

        p = {'numNeurons': sN, 'hist_dim': dh, 'numSamples': M, 'dt': 0.1, 'stim_dim': ds}
        return GLMPy(onp.concatenate((ow, oh, ob, ok), axis=None).flatten(), p)


    old = gen_ref()
    print(old.ll(data, stim))

