from abc import ABCMeta
from functools import reduce
from types import SimpleNamespace as Namespace

import numpy as np
import tensorflow as tf

from lib.layers import BatchNorm, Chain, Layer, LinTrans, Rect

################################################################################
# Optimization
################################################################################

def minimize_expected(net, cost, optimizer, lr_routing_scale=1.0):
    lr_scales = {
        **{θ: 1 / tf.sqrt(tf.reduce_mean(tf.square(ℓ.p_tr)))
           for ℓ in net.layers for θ in vars(ℓ.params).values()},
        **{θ: 1 / tf.sqrt(tf.reduce_mean(tf.square(ℓ.p_tr))) * lr_routing_scale
           for ℓ in net.layers for θ in vars(ℓ.router.params).values()}}
    grads = optimizer.compute_gradients(cost)
    scaled_grads = [(lr_scales[θ] * g, θ) for g, θ in grads if g is not None]
    return optimizer.apply_gradients(scaled_grads)

################################################################################
# Root Network Class
################################################################################

class Net(metaclass=ABCMeta):
    default_hypers = {}

    def __init__(self, x0_shape, y_shape, hypers, layers):
        full_hyper_dict = {**self.__class__.default_hypers, **hypers}
        self.hypers = Namespace(**full_hyper_dict)
        self.x0 = tf.placeholder(tf.float32, (None,) + x0_shape)
        self.y = tf.placeholder(tf.float32, (None,) + y_shape)
        self.mode = tf.placeholder_with_default('ev', ())
        self.sinks = {}
        def head(tree):
            return tree if isinstance(tree, Layer) else tree[0]
        def tail(tree):
            return [] if isinstance(tree, Layer) else tree[1:]
        def link(tree, x):
            source, sinks = head(tree), tail(tree)
            source.link(x, self.y, self.mode)
            source.sinks = list(map(head, sinks))
            for s in sinks:
                link(s, source.x)
        self.root = head(layers)
        link(layers, self.x0)

    @property
    def layers(self):
        def all_in_tree(layer):
            yield layer
            for sink in layer.sinks:
                yield from all_in_tree(sink)
        yield from all_in_tree(self.root)

    @property
    def leaves(self):
        return (ℓ for ℓ in self.layers if len(ℓ.sinks) == 0)

    def train(self, x0, y, hypers={}):
        pass

    def validate(self, x0, y, hypers={}):
        pass

    def eval(self, target, x0, y, hypers={}):
        pass

################################################################################
# Statically-Routed Networks
################################################################################

class SRNet(Net):
    def __init__(self, x0_shape, y_shape, optimizer, layers):
        super().__init__(x0_shape, y_shape, {}, layers)
        for ℓ in self.layers:
            ℓ.p_ev = tf.ones((tf.shape(ℓ.x)[0],))
        c_tr = sum(ℓ.c_err + ℓ.c_mod for ℓ in self.layers)
        self._train_op = optimizer.minimize(tf.reduce_mean(c_tr))
        self._sess = tf.Session()
        self._sess.run(tf.initialize_all_variables())

    def __del__(self):
        self._sess.close()

    def train(self, x0, y, hypers={}):
        self._sess.run(self._train_op, {
            self.x0: x0, self.y: y, self.mode: 'tr', **hypers})

    def eval(self, target, x0, y, hypers={}):
        return self._sess.run(target, {
            self.x0: x0, self.y: y, **hypers})

################################################################################
# Decision Smoothing Networks
################################################################################

def route_sinks_ds_stat(ℓ, opts):
    ℓ.router = Chain()
    ℓ.router.link(ℓ.x, None, opts.mode)
    for s in ℓ.sinks:
        route_ds(s, ℓ.p_tr, ℓ.p_ev, opts)

def route_sinks_ds_dyn(ℓ, opts):
    ℓ.router = opts.router_gen(ℓ)
    ℓ.router.link(ℓ.x, None, opts.mode)
    π_tr = (
        opts.ϵ / len(ℓ.sinks)
        + (1 - opts.ϵ) * tf.nn.softmax(ℓ.router.x))
    π_ev = tf.to_float(tf.equal(
        tf.expand_dims(tf.to_int32(tf.argmax(ℓ.router.x, 1)), 1),
        tf.range(len(ℓ.sinks))))
    for i, s in enumerate(ℓ.sinks):
        route_ds(s, ℓ.p_tr * π_tr[:, i], ℓ.p_ev * π_ev[:, i], opts)

def route_ds(ℓ, p_tr, p_ev, opts):
    ℓ.p_tr = p_tr
    ℓ.p_ev = p_ev
    ℓ.μ_tr = tf.Variable(0.0, trainable=False)
    ℓ.μ_vl = tf.Variable(0.0, trainable=False)
    ℓ.v_tr = tf.Variable(1.0, trainable=False)
    ℓ.v_vl = tf.Variable(1.0, trainable=False)
    μ_batch = (
        tf.reduce_sum(ℓ.p_tr * ℓ.c_err)
        / tf.reduce_sum(ℓ.p_tr))
    v_batch = (
        tf.reduce_sum(ℓ.p_tr * tf.square(ℓ.c_err - μ_batch))
        / tf.reduce_sum(ℓ.p_tr))
    ℓ.update_μv_tr = tf.group(
        tf.assign(ℓ.μ_tr, opts.λ * ℓ.μ_tr + (1 - opts.λ) * μ_batch),
        tf.assign(ℓ.v_tr, opts.λ * ℓ.v_tr + (1 - opts.λ) * v_batch))
    ℓ.update_μv_vl = tf.group(
        tf.assign(ℓ.μ_vl, opts.λ * ℓ.μ_vl + (1 - opts.λ) * μ_batch),
        tf.assign(ℓ.v_vl, opts.λ * ℓ.v_vl + (1 - opts.λ) * v_batch))
    ℓ.c_gen = (
        tf.sqrt((ℓ.v_vl + 1e-3) / (ℓ.v_tr + 1e-3))
        * (ℓ.c_err - ℓ.μ_tr) + ℓ.μ_vl)
    if len(ℓ.sinks) < 2: route_sinks_ds_stat(ℓ, opts)
    else: route_sinks_ds_dyn(ℓ, opts)

class DSNet(Net):
    default_hypers = dict(k_cpt=0.0, ϵ=0.1, λ=0.9)

    def __init__(self, x0_shape, y_shape, router_gen, optimizer, hypers, root):
        super().__init__(x0_shape, y_shape, hypers, root)
        n_pts = tf.shape(self.x0)[0]
        route_ds(self.root, tf.ones((n_pts,)), tf.ones((n_pts,)),
                 Namespace(router_gen=router_gen, mode=self.mode,
                           **vars(self.hypers)))
        c_gen = sum(ℓ.p_tr * ℓ.c_gen for ℓ in self.layers)
        c_cpt = sum(ℓ.p_tr * self.hypers.k_cpt * ℓ.n_ops for ℓ in self.layers)
        c_mod = sum(tf.stop_gradient(ℓ.p_tr) * (ℓ.c_mod + ℓ.router.c_mod)
                    for ℓ in self.layers)
        c_tr = c_gen + c_cpt + c_mod
        with tf.control_dependencies([ℓ.update_μv_tr for ℓ in self.layers]):
            self._train_op = minimize_expected(
                self, tf.reduce_mean(c_tr), optimizer)
        self._validate_op = tf.group(*(ℓ.update_μv_vl for ℓ in self.layers))
        self._sess = tf.Session()
        self._sess.run(tf.initialize_all_variables())

    def __del__(self):
        self._sess.close()

    def train(self, x0, y, hypers={}):
        self._sess.run(self._train_op, {
            self.x0: x0, self.y: y, self.mode: 'tr', **hypers})

    def validate(self, x0, y, hypers={}):
        self._sess.run(self._validate_op, {
            self.x0: x0, self.y: y, **hypers})

    def eval(self, target, x0, y, hypers={}):
        return self._sess.run(target, {
            self.x0: x0, self.y: y, **hypers})

################################################################################
# Cost Regression Networks
################################################################################

def route_sinks_cr_stat(ℓ, opts):
    ℓ.router = Chain()
    ℓ.router.link(ℓ.x, None, opts.mode)
    for s in ℓ.sinks:
        route_cr(s, ℓ.p_tr, ℓ.p_ev, opts)
    ℓ.c_ev = (
        ℓ.c_err + opts.k_cpt * ℓ.n_ops
        + sum(s.c_ev for s in ℓ.sinks))
    ℓ.c_opt = (
        ℓ.c_err + opts.k_cpt * ℓ.n_ops
        + sum(s.c_opt for s in ℓ.sinks))
    ℓ.c_cre = 0.0

def route_sinks_cr_dyn(ℓ, opts):
    ℓ.router = opts.router_gen(ℓ)
    ℓ.router.link(ℓ.x, None, opts.mode)
    π_ev = tf.to_float(tf.equal(
        tf.expand_dims(tf.to_int32(tf.argmin(ℓ.router.x, 1)), 1),
        tf.range(len(ℓ.sinks))))
    π_tr = opts.ϵ / len(ℓ.sinks) + (1 - opts.ϵ) * π_ev
    for i, s in enumerate(ℓ.sinks):
        route_cr(s, ℓ.p_tr * π_tr[:, i], ℓ.p_ev * π_ev[:, i], opts)
    ℓ.c_ev = (
        ℓ.c_gen + opts.k_cpt * ℓ.n_ops
        + sum(π_ev[:, i] * s.c_ev
              for i, s in enumerate(ℓ.sinks)))
    ℓ.c_opt = (
        ℓ.c_gen + opts.k_cpt * ℓ.n_ops
        + reduce(tf.minimum, (s.c_opt for s in ℓ.sinks)))
    if opts.optimistic:
        ℓ.c_cre = opts.k_cre * sum(
            π_tr[:, i] * tf.square(
                ℓ.router.x[:, i] - tf.stop_gradient(s.c_opt))
            for i, s in enumerate(ℓ.sinks))
    else:
        ℓ.c_cre = opts.k_cre * sum(
            π_tr[:, i] * tf.square(
                ℓ.router.x[:, i] - tf.stop_gradient(s.c_ev))
            for i, s in enumerate(ℓ.sinks))

def route_cr(ℓ, p_tr, p_ev, opts):
    ℓ.p_tr = p_tr
    ℓ.p_ev = p_ev
    ℓ.μ_tr = tf.Variable(0.0, trainable=False)
    ℓ.μ_vl = tf.Variable(0.0, trainable=False)
    ℓ.v_tr = tf.Variable(1.0, trainable=False)
    ℓ.v_vl = tf.Variable(1.0, trainable=False)
    μ_batch = (
        tf.reduce_sum(ℓ.p_tr * ℓ.c_err)
        / tf.reduce_sum(ℓ.p_tr))
    v_batch = (
        tf.reduce_sum(ℓ.p_tr * tf.square(ℓ.c_err - μ_batch))
        / tf.reduce_sum(ℓ.p_tr))
    ℓ.update_μv_tr = tf.group(
        tf.assign(ℓ.μ_tr, opts.λ * ℓ.μ_tr + (1 - opts.λ) * μ_batch),
        tf.assign(ℓ.v_tr, opts.λ * ℓ.v_tr + (1 - opts.λ) * v_batch))
    ℓ.update_μv_vl = tf.group(
        tf.assign(ℓ.μ_vl, opts.λ * ℓ.μ_vl + (1 - opts.λ) * μ_batch),
        tf.assign(ℓ.v_vl, opts.λ * ℓ.v_vl + (1 - opts.λ) * v_batch))
    ℓ.c_gen = (
        tf.sqrt((ℓ.v_vl + 1e-3) / (ℓ.v_tr + 1e-3))
        * (ℓ.c_err - ℓ.μ_tr) + ℓ.μ_vl)
    if len(ℓ.sinks) < 2: route_sinks_cr_stat(ℓ, opts)
    else: route_sinks_cr_dyn(ℓ, opts)

class CRNet(Net):
    default_hypers = dict(k_cpt=0.0, k_cre=1e-3, ϵ=0.1, λ=0.99,
                          optimistic=True)

    def __init__(self, x0_shape, y_shape, router_gen, optimizer, hypers, root):
        super().__init__(x0_shape, y_shape, hypers, root)
        n_pts = tf.shape(self.x0)[0]
        route_cr(self.root, tf.ones((n_pts,)), tf.ones((n_pts,)),
                 Namespace(router_gen=router_gen, mode=self.mode,
                           **vars(self.hypers)))
        c_gen = sum(ℓ.p_tr * ℓ.c_gen for ℓ in self.layers)
        c_cpt = sum(ℓ.p_tr * self.hypers.k_cpt * ℓ.n_ops for ℓ in self.layers)
        c_cre = sum(ℓ.p_tr * ℓ.c_cre for ℓ in self.layers)
        c_mod = sum(ℓ.p_tr * (ℓ.c_mod + ℓ.router.c_mod) for ℓ in self.layers)
        c_tr = c_gen + c_cpt + c_cre + c_mod
        with tf.control_dependencies([ℓ.update_μv_tr for ℓ in self.layers]):
            self._train_op = minimize_expected(
                self, tf.reduce_mean(c_tr), optimizer, 1 / self.hypers.k_cre)
        self._validate_op = tf.group(*(ℓ.update_μv_vl for ℓ in self.layers))
        self._sess = tf.Session()
        self._sess.run(tf.initialize_all_variables())

    def __del__(self):
        self._sess.close()

    def train(self, x0, y, hypers={}):
        self._sess.run(self._train_op, {
            self.x0: x0, self.y: y, self.mode: 'tr', **hypers})

    def validate(self, x0, y, hypers={}):
        self._sess.run(self._validate_op, {
            self.x0: x0, self.y: y, **hypers})

    def eval(self, target, x0, y, hypers={}):
        return self._sess.run(target, {
            self.x0: x0, self.y: y, **hypers})
