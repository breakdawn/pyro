import argparse
import torch
import torch.nn as nn
import numpy as np

import pyro
import pyro.distributions as dist
from pyro import optim
from pyro.infer import Trace_ELBO
from pyro.contrib.oed.eig import vi_ape, donsker_varadhan_loss, naive_rainforth

# torch.set_default_tensor_type('torch.DoubleTensor')

"""
Example builds on the Bayesian regression tutorial [1]. It demonstrates how
to estimate the average posterior entropy (APE) under a model and use it to
make an optimal decision about experiment design.

The context is a Gaussian linear model in which the design matrix `X` is a
one-hot-encoded matrix with 2 columns. This corresponds to the simplest form
of an A/B test. Assume no data has yet be collected. The aim is to find the optimal
allocation of participants to the two groups to maximise the expected gain in
information from actually performing the experiment.

For details of the implementation of average posterior entropy estimation, see
the docs for :func:`pyro.contrib.oed.eig.vi_ape`.

We recommend the technical report from Long Ouyang et al [3] as an introduction
to optimal experiment design within probabilistic programs.

[1] ["Bayesian Regression"](http://pyro.ai/examples/bayesian_regression.html)
[2] Long Ouyang, Michael Henry Tessler, Daniel Ly, Noah Goodman (2016),
    "Practical optimal experiment design with probabilistic programs",
    (https://arxiv.org/abs/1608.05046)
"""

# Set up regression model dimensions
N = 10  # number of participants
p_treatments = 2  # number of treatment groups
p = p_treatments  # number of features
prior_stdevs = torch.tensor([1, .5])

softplus = torch.nn.functional.softplus


def model(design):
    # Allow batching of designs
    loc_shape = list(design.shape)
    loc_shape[-2] = 1
    loc = torch.zeros(loc_shape)
    scale = prior_stdevs
    # Place a normal prior on the regression coefficient
    # w is 1 x p: hence use .independent(2)
    w_prior = dist.Normal(loc, scale).independent(2)
    w = pyro.sample('w', w_prior).transpose(-1, -2)

    # Run the regressor forward conditioned on inputs
    prediction_mean = torch.matmul(design, w).squeeze(-1)
    # y is an n-vector: hence use .independent(1)
    pyro.sample("y", dist.Normal(prediction_mean, 1).independent(1))


def guide(design):
    # Guide defines a Gaussian family for `w`
    # The true posterior for `w` is within this family
    # In this case, variational inference will be exact
    loc_shape = list(design.shape)
    loc_shape[-2] = 1
    # define our variational parameters
    w_loc = torch.zeros(loc_shape)
    # note that we initialize our scales to be pretty narrow
    w_sig = -3*torch.ones(loc_shape)
    # register learnable params in the param store
    mw_param = pyro.param("guide_mean_weight", w_loc)
    sw_param = softplus(pyro.param("guide_scale_weight", w_sig))
    # guide distributions for w
    w_dist = dist.Normal(mw_param, sw_param).independent(2)
    pyro.sample('w', w_dist)


class DVNeuralNet(nn.Module):
    def __init__(self, design_dim, y_dim):
        super(DVNeuralNet, self).__init__()
        input_dim = design_dim + 2*y_dim + 1
        self.linear1 = nn.Linear(input_dim, input_dim)
        self.linear2 = nn.Linear(input_dim, input_dim)
        self.linear3 = nn.Linear(input_dim, 1)
        self.softplus = nn.Softplus()

    def forward(self, design, y, lp):
        design_suff = design.sum(-2, keepdim=True)
        suff = torch.matmul(y.unsqueeze(-2), design)
        squares = torch.matmul((y**2).unsqueeze(-2), design)
        allsquare = suff**2
        lp_unsqueezed = lp.unsqueeze(-1).unsqueeze(-2)
        m = torch.cat([squares, allsquare, design_suff, lp_unsqueezed], -1)
        h1 = self.softplus(self.linear1(m))
        h2 = self.softplus(self.linear2(h1))
        o = self.linear3(h2).squeeze(-2).squeeze(-1)
        return o


def design_to_matrix(design):
    """Converts a one-dimensional tensor listing group sizes into a
    two-dimensional binary tensor of indicator variables.

    :return: A :math:`n \times p` binary matrix where :math:`p` is
        the length of `design` and :math:`n` is its sum. There are
        :math:`n_i` ones in the :math:`i`th column.
    :rtype: torch.tensor

    """
    n, p = int(torch.sum(design)), int(design.size()[0])
    X = torch.zeros(n, p)
    t = 0
    for col, i in enumerate(design):
        i = int(i)
        if i > 0:
            X[t:t+i, col] = 1.
        t += i
    return X


def analytic_posterior_entropy(prior_cov, x):
    posterior_cov = prior_cov - prior_cov.mm(x.t().mm(torch.inverse(
        x.mm(prior_cov.mm(x.t())) + torch.eye(N)).mm(x.mm(prior_cov))))
    return 0.5*torch.logdet(2*np.pi*np.e*posterior_cov)


def main(num_steps):

    pyro.set_rng_seed(42)
    pyro.clear_param_store()

    ns = range(0, N, 2)
    designs = [design_to_matrix(torch.tensor([n1, N-n1])) for n1 in ns]
    X = torch.stack(designs)

    # Analytic loss
    true_ape = []
    prior_cov = torch.diag(prior_stdevs**2)
    H_prior = 0.5*torch.logdet(2*np.pi*np.e*prior_cov)
    for i in range(len(ns)):
        x = X[i, :, :]
        true_ape.append(analytic_posterior_entropy(prior_cov, x))

    true_ape = torch.tensor(true_ape)
    print("True APE")
    print(true_ape)
    print("True EIG")
    print(H_prior - true_ape)
    
    rainforth = naive_rainforth(model, X, "y", "w", N=10000, M=100)
    print("10000-100 Rainforth estimate")
    print(rainforth)
    
    # Donsker varadhan
    dv_loss_fn = donsker_varadhan_loss(model, X, "y", "w", 100,
                                       DVNeuralNet(p, p))
    params = None
    ewma = None
    alpha=1000.
    opt = optim.Adam({"lr": 0.0025})
    for step in range(300000):
        if params is not None:
            pyro.infer.util.zero_grads(params)
        agg_loss, dv_loss = dv_loss_fn()
        if ewma is None:
            ewma = dv_loss
        else:
            ewma = (1/(1+alpha))*(dv_loss + alpha*ewma)
        if step % 1000 == 0:
            print("EWMA loss")
            print(ewma)
        agg_loss.backward()
        params = [pyro.param(name).unconstrained()
                  for name in pyro.get_param_store().get_all_param_names()]
        opt(params)

    # Estimated loss (linear transform of EIG)
    est_ape = vi_ape(
        model,
        X,
        observation_labels="y",
        vi_parameters={
            "guide": guide,
            "optim": optim.Adam({"lr": 0.0025}),
            "loss": Trace_ELBO(),
            "num_steps": num_steps},
        is_parameters={"num_samples": 2}
    )

    print("Nested VI APE values")
    print(est_ape)

    # # Plot to compare
    # import matplotlib.pyplot as plt
    # ns = np.array(ns)
    # est_ape = np.array(est_ape.detach())
    # true_ape = np.array(true_ape)
    # plt.scatter(ns, est_ape)
    # plt.scatter(ns, true_ape, color='r')
    # plt.show()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="A/B test experiment design using VI")
    parser.add_argument("-n", "--num-steps", nargs="?", default=3000, type=int)
    args = parser.parse_args()
    main(args.num_steps)
