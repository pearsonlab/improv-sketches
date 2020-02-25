# -*- coding: utf-8 -*-

from functools import partial
from importlib import import_module
from typing import Dict, Tuple

import jax.numpy as np
import numpy as onp
from jax import devices, jit, random, value_and_grad
from jax.config import config
from jax.experimental.optimizers import OptimizerState
from jax.interpreters.xla import DeviceArray

config.update("jax_enable_x64", True)

class GLMJax:
    def __init__(self, p: Dict, θ=None, optimizer=None, use_gpu=False):
        """
        A JAX implementation of simGLM.

        Assuming that all parameters are fixed except N and M.
        - N can be increased forever (at a cost).
        - M can only be increased to M_lim.

        There is a substantial cost to increasing N. It is a better idea to keep N reasonably high at initialization.

        :param p: Dictionary of parameters.
        :type p: dict
        :param θ: Dictionary of ndarray weights. Must conform to parameters in p.
        :type θ: dict
        :param optimizer: Dictionary of optimizer name and hyperparameters from jax.experimental.optimizers. Ex. {'name': 'SGD, **kwargs}
        :type optimizer: dict
        :param use_gpu: Use GPU
        :type use_gpu: bool
        """

        self.use_gpu = use_gpu
        platform = 'gpu' if use_gpu else 'cpu'  # If changing platform, python interpreter needs to restart.
        config.update('jax_platform_name', platform)
        print('Using', devices()[0])

        if not self.use_gpu:
            config.update("jax_enable_x64", True)

        # p check
        if not all(k in p for k in ['ds', 'dh', 'dt', 'N_lim', 'M_lim']):
            raise ValueError('Parameter incomplete!')
        self.params = p

        # θ check
        if θ is None:
            w = np.zeros((p['N_lim'], p['N_lim']))
            k = np.zeros((p['N_lim'], p['ds']))
            b = np.zeros((p['N_lim'], 1))
            h = np.zeros((p['N_lim'], p['dh']))
            self._θ = {'h': h, 'w': w, 'b': b, 'k': k}

        else:
            assert θ['w'].shape == (p['N_lim'], p['N_lim'])
            assert θ['h'].shape == (p['N_lim'], p['dh'])
            assert θ['k'].shape == (p['N_lim'], p['ds'])
            assert (θ['b'].shape == (p['N_lim'],)) or (θ['b'].shape == (p['N_lim'], 1))

            if len(θ['b'].shape) == 1:  # Array needs to be 2D.
                θ['b'] = np.reshape(θ['b'], (p['N_lim'], 1))

            self._θ = {key: np.asarray(item) for key, item in θ.items()}  # Convert to DeviceArray

        # Setup optimizer
        if optimizer is None:
            optimizer = {'name': 'sgd', 'step_size': 1e-5}
        print(f'Optimizer: {optimizer}')
        opt_func = getattr(import_module('jax.experimental.optimizers'), optimizer['name'])
        optimizer = {k: v for k, v in optimizer.items() if k != 'name'}
        self.opt_init, self.opt_update, self.get_params = opt_func(**optimizer)
        self._θ: OptimizerState = self.opt_init(self._θ)

        self._ll_grad = value_and_grad(self._ll)

        self.current_N = 0
        self.current_M = 0
        self.iter = 0

    def ll(self, y, s) -> float:
        args = self._check_arrays(y, s)
        return float(self._ll(self.θ, self.params, *args))

    def fit(self, y, s) -> float:
        args = self._check_arrays(y, s)
        ll, Δ = self._ll_grad(self.θ, self.params, *args)
        self._θ: OptimizerState = self.opt_update(self.iter, Δ, self._θ)

        self.iter += 1
        return float(ll)

    def get_grad(self, y, s) -> DeviceArray:
        return self._ll_grad(self.θ, self.params, *self._check_arrays(y, s))[1]

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

        return (np.sum(r̂) - correction - np.sum(y * np.log(r̂ + np.finfo(np.float64).eps))) / curr_mn

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
        return f'simGLM({self.params}, θ={self.θ}, optimizer={self.optimizer}, gpu={self.use_gpu})'

    def __str__(self):
        return f'simGLM Iteration: {self.iter}, Optimizer: {self.optimizer} \n Parameters: {self.params})'


def online_sch(i_frame):
    """ Learning rate schedule for JAX. Linear increase over frame number. """
    print(i_frame)
    return 1e-5 * i_frame / 100


if __name__ == '__main__':  # Test
    from glm_py import GLMPy

    N = 5
    M = 20
    dh = 2
    ds = 8
    p = {'N': N, 'M': M, 'dh': dh, 'ds': ds, 'dt': 0.1, 'n': 0, 'N_lim': N, 'M_lim': M}

    # w = np.zeros((N, N))
    # h = np.zeros((N, dh))
    # k = np.zeros((N, ds))
    # b = np.zeros((N, 1))

    key = random.PRNGKey(10)
    w = random.normal(key, shape=(N, N)) * 0.001
    h = random.normal(key, shape=(N, dh)) * 0.001
    k = random.normal(key, shape=(N, ds)) * 0.001
    b = random.normal(key, shape=(N, 1)) * 0.001

    theta = {'h': np.flip(h, axis=1), 'w': w, 'b': b, 'k': k}
    model = GLMJax(p, theta, optimizer={'name': 'sgd', 'step_size': 1e-5})# online_sch})

    onp.random.seed(10)
    data = onp.random.randn(N, M)  # onp.zeros((8, 50))
    stim = onp.random.randn(ds, M)

    def gen_ref():
        ow = onp.asarray(w)[:N, :N]
        oh = onp.asarray(h)[:N, :]
        ok = onp.asarray(k)[:N, :]
        ob = onp.asarray(b)[:N, :]
        p = {'numNeurons': N, 'hist_dim': dh, 'numSamples': M, 'dt': 0.1, 'stim_dim': ds}
        return GLMPy(onp.concatenate((ow, oh, ob, ok), axis=None).flatten(), p)

    old = gen_ref()
    old.S = data
    old.currStimID = stim

    print('Original LL')
    print(model.ll(data, stim))
    print(old.ll(data, stim))

    print('Original grad - w')
    print(model.get_grad(data, stim)['w'])
    print(old.ll_grad(data, stim)[:N*N].reshape(N, N))

    for i in range(100):
        old.fit()
        model.fit(data, stim)

    print('LL after 100 steps.')
    print(model.ll(data, stim))
    print(old.ll(data, stim))
