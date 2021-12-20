### Learners ###

# Note, the RMAPPO code is primarily taken from work by Kamal Ndousse
# His original repository can be found at https://github.com/kandouss/kamarl and is licensed under the MIT License (also included for reference within this repository)

import torch
from torch.distributions.transforms import PowerTransform
import torch.tensor as tt
import random
from torch import pow as power
import time
from collections import namedtuple, deque
import torch.nn as nn
from torch import optim, ones_like
from torch.distributions import Categorical
from torch.nn.functional import one_hot as one_hot
import cProfile
import itertools
import numpy as np
from os import path
import pickle
import specs
import envs
import utils


# Use GPU if available
if torch.cuda.is_available():
    # print("Using GPU!")
    device = torch.device("cuda")
    torch.set_default_tensor_type('torch.cuda.FloatTensor')
else:
    # print("Using CPU!")
    device = torch.device("cpu")


# Basic DNN module
class DNN(nn.Module):

    def __init__(self, in_size, out_sizes, output, hidden, bias=True):

        super(DNN,self).__init__()

        self.dims = [in_size] + hidden + [sum(out_sizes)]
        self.output = output
        self.out_sizes = out_sizes
        self.layers = nn.ModuleList([])

        for i in range(len(self.dims) - 1):
            self.layers.append(nn.Linear(self.dims[i], self.dims[i+1], bias=bias))

    def forward(self, x):

        # Apply to ReLU activations to all layers apart from the last
        x = x.float()
        for l in self.layers[:-1]:
            x = nn.functional.relu(l(x))

        # Compute last layer
        x = x.float()
        x = self.layers[-1](x)

        # Return output of DNN as a tuple
        if self.output == 'linear':
            return x
        elif self.output == 'softmax':
            return nn.functional.softmax(x,dim=-1)
            # return [nn.functional.softmax(y,dim=-1) for y in torch.split(x, self.out_sizes, dim=d)]
        elif self.output == 'actors':
            pass
        elif self.output == 'gaussian':
            pass
        else:
            print("Error: DNN output must be \'linear\' or \'softmax\' or \'beta\' or \'gaussian\'")


# Basic LSTM module
class LSTM(nn.Module):

    @torch.jit.script
    def lstm_forward(X, hx, weight_ih, weight_hh, bias_ih, bias_hh):
        hx = hx.unsqueeze(-2)
        
        for k, x in enumerate(X.unbind(-2)):
            hx_ = torch.stack(torch._VF.lstm_cell( x, hx[...,k,:].unbind(0),
                                weight_ih, weight_hh, bias_ih, bias_hh))
            hx = torch.cat((hx, hx_.unsqueeze(2)), dim=-2)

        return hx[:, :, 1:]

    class SeqLSTM(nn.RNNCellBase):
        """ The built-in PyTorch RNN modules only return the hidden states for the final step in an input 
        sequence. This LSTM module returns all the intermediate hidden states, and does so by looping over the
        intermediate hidden states. The actual looping is accelerated by the jit-compiled method `lstm_forward`
        """

        def __init__(self, input_size, hidden_size):

            super().__init__(input_size=input_size, hidden_size=hidden_size, bias=True, num_chunks=4)
            self.input_size = input_size
            self.hidden_size = hidden_size
            self.hx_cx = self.ac.empty_hidden()

        def forward(self, X, hx=None):

            out_shape = (*X.shape[:-1], self.hidden_size)
            while X.dim() < 3:
                X = X.unsqueeze(0)

            self.check_forward_input(X[:,0,:])
            
            if hx is None:
                hx = torch.zeros(2, X.size(0), self.hidden_size, dtype=X.dtype, device=X.device)
            else:
                assert len(hx) == 2
                if isinstance(hx, tuple):
                    hx = torch.stack(hx)
                if hx.dim() == 2: # If the hidden state has no batch dimension, make one up
                    hx = hx.unsqueeze(1)
                try:
                    self.check_forward_hidden(X[:,0,:], hx[0], '[0]')
                    self.check_forward_hidden(X[:,0,:], hx[1], '[1]')
                except:
                    import pdb; pdb.set_trace()

            res = self.lstm_forward(
                X, hx,
                self.weight_ih, self.weight_hh,
                self.bias_ih, self.bias_hh,
            )

            return res.reshape((2, *out_shape))
        
        def empty_hidden(self):
        
            return torch.zeros((2, self.hidden_size),
                                dtype=torch.float32,
                                device=next(self.parameters()).device)

    def __init__(self, in_size, pol_out_size, val_out_size, pre_hidden, lstm, act_hidden, val_hidden):

        super(LSTM,self).__init__()

        self.in_size = in_size
        self.pol_out_sizes = pol_out_size
        self.val_out_size = val_out_size
        self.pre_dnn = DNN(in_size, lstm[0], 'linear', pre_hidden)
        self.lstm = self.SeqLSTM(input_size=lstm[0], hidden_size=lstm[1])
        self.act_dnn = DNN(self.lstm.hidden_size, pol_out_size, 'softmax', act_hidden)
        self.val_dnn = DNN(self.lstm.hidden_size, val_out_size, 'linear', val_hidden)

    def forward(self, x, hx=None, return_old_hidden=False):
        
        if hx == None:
            hx = self.lstm.hx_cx
        old_hidden = self.lstm.hx_cx.detach()
        x = self.pre_dnn(x)
        x, hx = self.lstm(x, hx)
        self.lstm.hx_cx = torch.stack((x, hx))

        if return_old_hidden:
            return self.act_dnn(x), self.val_dnn(x), old_hidden
        else:
            return self.act_dnn(x), self.val_dnn(x)
        
    def reset(self):

        self.lstm.hx_cx = self.lstm.empty_hidden()

    def get_hidden_state(self):

        return self.lstm.hx_cx.detach()

    
# Agent buffer
class AgentBuffer:

    def __init__(self, buffer_size, batch_size):

        self.memory = deque(maxlen=buffer_size)
        self.batch_size = batch_size
        self.experiences = namedtuple("Experience", 
                                        field_names=["time",
                                                    "state",
                                                    "hx_cx",
                                                    "joint_action",
                                                    "rewards",
                                                    "next_state",
                                                    "next_hx_cx",
                                                    "gamma",
                                                    "discount",
                                                    "done",
                                                    ])     

    def add(self, time, state, hx_cx, joint_action, reward, next_state, next_hx_cx, gamma, discount, done):
        
        e = self.experiences(time, state, hx_cx, joint_action, reward, next_state, next_hx_cx, gamma, discount, done)
        self.memory.append(e)
        
    def sample(self, sample_all=False):

        if len(self.memory) == 0:
            return None
        elif sample_all or len(self.memory) < self.batch_size:
            experiences = self.memory
        else:
            experiences = random.sample(self.memory, k=self.batch_size)

        times = tt([e.time for e in experiences if e is not None],dtype=torch.long).view(-1,1).to(device)
        states = torch.stack([e.state for e in experiences if e is not None]).float().to(device)
        joint_actions = [tt([e.joint_action[i] for e in experiences if e is not None],dtype=torch.long).view(-1,1).to(device) for i in range(len(experiences[0].joint_action))]
        rewards = [tt([e.reward[j] for e in experiences if e is not None]).view(-1,1).float().to(device) for j in range(len(experiences[0].reward))]
        next_states = torch.stack([e.next_state for e in experiences if e is not None]).float().to(device)
        gammas = [tt([e.gamma[j] for e in experiences if e is not None]).view(-1,1).float().to(device) for j in range(len(experiences[0].gamma))]
        discounts = [tt([e.discount[j] for e in experiences if e is not None]).view(-1,1).float().to(device) for j in range(len(experiences[0].discount))]
        dones = tt([e.done for e in experiences if e is not None],dtype=torch.int8).view(-1,1).to(device)
        hx_cxs = [tt([e.hx_cx[i] for e in experiences if e is not None]).view(-1,1).to(device) for i in range(len(experiences[0].joint_action))]
        next_hx_cxs = [tt([e.next_hx_cx[i] for e in experiences if e is not None]).view(-1,1).to(device) for i in range(len(experiences[0].joint_action))]

        return (times, states, hx_cxs, joint_actions, rewards, next_states, next_hx_cxs, gammas, discounts, dones)
    
    def clear(self):

        self.memory.clear()
    
    def __len__(self):
        return len(self.memory)
    


# Critic buffer module
class CriticBuffer:

    def __init__(self, buffer_size, batch_size):

        self.memory = deque(maxlen=buffer_size)
        self.batch_size = batch_size
        self.experiences = namedtuple("Experience", 
                                        field_names=["time",
                                                    "state",
                                                    "joint_action",
                                                    "reward",
                                                    "next_state",
                                                    "discount",
                                                    "gamma",
                                                    "done"])     

    def add(self, time, state, joint_action, reward, next_state, discount, gamma, done):
        
        e = self.experiences(time, state, joint_action, reward, next_state, discount, gamma, done)
        self.memory.append(e)
        
    def sample(self, sample_all=False):

        if len(self.memory) == 0:
            return None
        elif sample_all or len(self.memory) < self.batch_size:
            experiences = self.memory
        else:
            experiences = random.sample(self.memory, k=self.batch_size)

        times = tt([e.time for e in experiences if e is not None],dtype=torch.long).view(-1,1).to(device)
        states = torch.stack([e.state for e in experiences if e is not None]).float().to(device)
        joint_actions = [tt([e.joint_action[i] for e in experiences if e is not None],dtype=torch.long).view(-1,1).to(device) for i in range(len(experiences[0].joint_action))]
        rewards = tt([e.reward for e in experiences if e is not None]).view(-1,1).float().to(device)
        next_states = torch.stack([e.next_state for e in experiences if e is not None]).float().to(device)
        discounts = tt([e.discount for e in experiences if e is not None]).view(-1,1).float().to(device)
        gammas = tt([e.gamma for e in experiences if e is not None]).view(-1,1).float().to(device)
        dones = tt([e.done for e in experiences if e is not None],dtype=torch.int8).view(-1,1).to(device)
        
        return (times, states, joint_actions, rewards, next_states, discounts, gammas, dones)
    
    def clear(self):

        self.memory.clear()
    
    def __len__(self):
        return len(self.memory)


# Actor buffer module
class ActorBuffer:

    def __init__(self, buffer_size, batch_size):

        self.memory = deque(maxlen=buffer_size)
        self.batch_size = batch_size
        self.experiences = namedtuple("Experience", 
                                        field_names=["time",
                                                    "state",
                                                    "joint_action",
                                                    "reward",
                                                    "next_state",
                                                    "discount",
                                                    "gamma",
                                                    "done",
                                                    "true_state"])     

    def add(self, time, state, joint_action, reward, next_state, discount, gamma, done, true_state):
        
        e = self.experiences(time, state, joint_action, reward, next_state, discount, gamma, done, true_state)
        self.memory.append(e)
        
    def sample(self, sample_all=False):

        if len(self.memory) == 0:
            return None
        elif sample_all or len(self.memory) < self.batch_size:
            experiences = self.memory
        else:
            experiences = random.sample(self.memory, k=self.batch_size)

        times = tt([e.time for e in experiences if e is not None],dtype=torch.long).view(-1,1).to(device)
        states = torch.stack([e.state for e in experiences if e is not None]).float().to(device)
        joint_actions = [tt([e.joint_action[i] for e in experiences if e is not None],dtype=torch.long).view(-1,1).to(device) for i in range(len(experiences[0].joint_action))]
        rewards = [tt([e.reward[j] for e in experiences if e is not None]).view(-1,1).float().to(device) for j in range(len(experiences[0].reward))]
        next_states = torch.stack([e.next_state for e in experiences if e is not None]).float().to(device)
        gammas = [tt([e.gamma[j] for e in experiences if e is not None]).view(-1,1).float().to(device) for j in range(len(experiences[0].gamma))]
        discounts = [tt([e.discount[j] for e in experiences if e is not None]).view(-1,1).float().to(device) for j in range(len(experiences[0].discount))]
        dones = tt([e.done for e in experiences if e is not None],dtype=torch.int8).view(-1,1).to(device)
        true_states = torch.stack([e.true_state for e in experiences if e is not None]).float().to(device)

        return (times, states, joint_actions, rewards, next_states, discounts, gammas, dones, true_states)
    
    def clear(self):

        self.memory.clear()
    
    def __len__(self):
        return len(self.memory)


# Set up actor functions 
def set_up_actors(parametrization, obs_size, act_sizes, shape):

    if parametrization == 'dnn':
        actors = []
        hidden = shape
        for i in range(len(act_sizes)):
            actor = DNN(obs_size, [act_sizes[i]], 'softmax', hidden)
            actors.append(actor)
    elif parametrization == 'linear':
        actors = []
        for i in range(len(act_sizes)):
            actor = DNN(obs_size, [act_sizes[i]], 'softmax', [], bias=False)
            actors.append(actor)
    elif parametrization == 'lstm':
        actors = []
        pre = shape[0]
        memory = shape[1]
        post = shape[2]
        for i in range(len(act_sizes)):
            actor = LSTM(obs_size, [act_sizes[i]], 'softmax', pre, memory, post)
            actors.append(actor)  
    else:
        print("Error: Actor must be \'dnn\' or \'linear\' or \'lstm\')")
    for actor in actors:
        actor.to(device)

    return actors


# Set up critic functions 
def set_up_critics(parametrization, obs_size, num_objectives, shape):

    if parametrization == 'dnn':
        hidden = shape
        critics = [DNN(obs_size, [1], 'linear', hidden) for j in range(num_objectives)]
    elif parametrization == 'linear':
        critics = [DNN(obs_size, [1], 'linear', [], bias=False) for j in range(num_objectives)]
    elif parametrization == 'lstm':
        pre = shape[0]
        memory = shape[1]
        post = shape[2]
        critics = [LSTM(obs_size, [1], 'linear', pre, memory, post) for j in range(num_objectives)]
    else:
        print("Error: Critic must be \'dnn\' or \'linear\' or \'lstm\')")
    for critic in critics:
        critic.to(device)

    return critics


# Set up optimisers for given networks
def set_up_optimisers(networks, optimiser, l2=0.1):

    if optimiser == 'adam':
        optimisers = [optim.Adam(n.parameters(), weight_decay=l2) for n in networks]
    elif optimiser == 'sgd':
        optimisers = [optim.SGD(n.parameters(), lr=1.0, weight_decay=l2) for n in networks]
    elif optimiser == 'rmsprop':
        optimisers = [optim.RMSprop(n.parameters(), weight_decay=l2) for n in networks] 
    else:
        print("Error: Optimiser must be \'adam\' or \'sgd\' or \'rmsprop\')")

    return optimisers


# Set up learning rates
def set_up_lrs(learning_rates):
    
    def make_lr_function(lr_settings):

        (method, param) = lr_settings

        if method == 'robbinsmonro':
            if not (0.5 < param) and (param < 1.0):
                print("Error: Robbins-Monro decay parameter p must satisfy 0.5 < p < 1.0")
                return
            lr_lambda = lambda e: e ** (-param)
        elif method == 'exponential':
            if not (0.0 < param) and (param < 1.0):
                print("Error: Exponential decay parameter p must satisfy 0.0 < p < 1.0")
                return
            lr_lambda = lambda e: param ** e
        elif method == 'constant':
            if not (0.0 < param):
                print("Error: Contant decay parameter p must satisfy 0.0 < p")
                return
            lr_lambda = lambda e: param
        else:
            print("Error: Learning rate must be \'robbinsmonro\' or \'exponential\' or \'constant\'")

        return lr_lambda

    lrs = dict()
    for k in learning_rates.keys():
        if k == 'critics':
            lrs[k] = make_lr_function(learning_rates[k])
        else:
            lrs[k] = []
            for lr in (learning_rates[k]):
                lrs[k].append(make_lr_function(lr))

    return lrs


# Almanac module
class Almanac:

    def __init__(self, obs_size, act_sizes, objectives, hps, load_from=None):
        
        # TODO
        if load_from != None:
            pass
        else:
            pass

        self.name = 'almanac'

        # Parameters
        self.act_sizes = act_sizes
        self.hps = hps
        self.lrs = set_up_lrs(self.hps['learning_rates'])
        self.objectives = objectives
        self.num_objectives = len(objectives)
        self.num_players = len(self.act_sizes)
        self.obs_size = obs_size

        # Values to maintain
        self.updates = {'critics':[0 for _ in range(self.num_objectives)], 'actors':0}
        self.gammas = self.num_objectives * [1.0]
        self.u = [0.0 for _ in range(self.num_objectives - 1)]
        self.k = 0 if self.hps['sequential'] else None
        self.recent_utilities = [deque(maxlen=25) for i in range(self.num_objectives)]
        self.Z = [dict() for _ in range(self.num_objectives)]
        self.kl_weight = 1.0
       
        # Networks
        self.actors = set_up_actors(self.hps['models']['actors']['type'], self.obs_size, self.act_sizes, shape=self.hps['models']['actors']['shape'])
        self.critics = set_up_critics(self.hps['models']['critics']['type'], self.obs_size, self.num_objectives, shape=self.hps['models']['critics']['shape'])
        
        # Optimisers
        self.actor_optimisers = set_up_optimisers(self.actors, self.hps['optimisers']['actors'], l2=self.hps['l2_weight'])
        self.critic_optimisers = set_up_optimisers(self.critics, self.hps['optimisers']['critics'], l2=self.hps['l2_weight'])

        # Buffers
        self.critic_buffers = [CriticBuffer(buffer_size=self.hps['buffers']['critics']['size'], batch_size=self.hps['buffers']['critics']['batch']) for _ in self.critics]
        self.actor_buffer = ActorBuffer(buffer_size=self.hps['buffers']['actors']['size'], batch_size=self.hps['buffers']['actors']['batch'])

        # Lagrange multipliers
        self.lagrange_multipliers = (self.num_objectives - 1) * [tt(0.0)]
        
    def update_critic(self, j, data, until_converged=True, num_updates=None):

        times, states, _, rewards, next_states, discounts, gammas, dones = data
        self.critics[j].train()

        # Form state dists for weighting samples
        dist = self.state_dist(times, gammas).to(device)

        # Compute target
        with torch.no_grad():
            target = rewards.to(device) + (discounts * self.critics[j](next_states.to(device)) * (1-dones)).to(device)

        # Update networks
        finished = False
        temp_recent_losses = deque(maxlen=25)
        if num_updates == None:
            num_updates = len(times)
        e = 0
        while not finished:

            # Form loss
            prediction = self.critics[j](states.to(device)).to(device)
            loss = self.lrs['critics'](e) * ((dist * (prediction - target)**2).mean()).to(device)

            # Backpropagate
            self.critic_optimisers[j].zero_grad()
            loss.backward()
            self.critic_optimisers[j].step()
            self.critics[j].eval()

            # Check whether to finish updating
            e += 1
            temp_recent_losses.append(loss)
            if (not until_converged and e > num_updates) or utils.converged(temp_recent_losses):
                finished = True
    
    def update_actors(self, data, objectives, until_converged=True, num_updates=None):

        with torch.no_grad():

            times, states, joint_actions, rewards, next_states, discounts, gammas, dones, true_states = data
            for i in range(self.num_players):
                self.actors[i].train()

            # Form advantages
            advantages = [rewards[j].to(device) + (discounts[j] * self.critics[j](next_states.to(device)) * (1-dones)).to(device) - self.critics[j](states.to(device)) for j in objectives]

            if self.hps['normalise_advantages']:
                advs = [(a - a.mean()) / (a.std() + 1e-8) for a in advantages]
            else:
                advs = advantages
            
            # Form old probability factors
            old_dist_probs = self.policy(true_states,probs=True)
            old_action_log_probs = sum([torch.gather(torch.log(old_dist_probs[i]), 1, joint_actions[i]) for i in range(self.num_players)])

            # Form state dists for weighting samples
            dists = [self.state_dist(times, gammas[j]).to(device) for j in objectives]
        
            # Update networks
            finished = False
            temp_recent_losses = deque(maxlen=25)
            if num_updates == None:
                num_updates = len(times)
            e = 0

        while not finished:

            # Form weights
            objective_range = self.k + 1 if self.k != None else self.num_objectives
            objective_weights = self.compute_weights(objective_range, e)

            # Form loss
            new_dist_probs = self.policy(true_states,probs=True)
            new_action_log_probs = sum([torch.gather(torch.log(new_dist_probs[i]), 1, joint_actions[i]) for i in range(self.num_players)])

            # log_ratio = (new_action_log_probs.masked_fill(new_action_log_probs == -np.inf, 0.0) - old_action_log_probs).to(device)

            log_ratio = new_action_log_probs - old_action_log_probs
            ratio = torch.exp(log_ratio)
            kl_penalty = ratio * log_ratio

            if self.hps['entropy_weight'] != 0.0:
                entropy_penalty = sum([torch.unsqueeze((p * torch.log(p)).sum(dim=1), 1) for p in new_dist_probs])
            else:
                entropy_penalty = 0.0

            obj_losses = [(dists[k] * (ratio * advs[k] - self.kl_weight * kl_penalty + self.hps['entropy_weight'] * entropy_penalty)).mean() for k in range(self.num_objectives)]

            loss = - sum([objective_weights[k] * obj_losses[k] for k in range(self.num_objectives)])

            # Backpropagate
            for i in range(self.num_players):
                self.actor_optimisers[i].zero_grad()
            loss.backward(retain_graph=True)
            for i in range(self.num_players):
                self.actor_optimisers[i].step()
                self.actors[i].eval()

            with torch.no_grad():

                # Used to catch when NaNs occur in the policy network(s)
                joint_action = [actions.sample() for actions in self.policy(states[0])]

                # Update KL weight term as in the original PPO paper
                mean_kl = kl_penalty.mean()
                if mean_kl < self.hps['kl_target'] / 1.5:
                    self.kl_weight *= 0.5
                elif mean_kl > self.hps['kl_target'] * 1.5:
                    self.kl_weight *= 2
                    # Use early stopping if KL divergence is too high
                    finished = True

                # Update Lagrange multipliers
                for k in range(self.num_objectives):
                    self.recent_utilities[k].append(obj_losses[k])
                self.update_lagrange_multipliers(e)

                # Check whether to finish updating
                e += 1
                if e > 1000:
                    finished = True
                temp_recent_losses.append(loss)
                if until_converged and not finished:
                    if self.k == None:
                        finished = utils.converged(temp_recent_losses)
                    elif self.k == self.num_objectives - 1 and utils.converged(self.recent_utilities[self.k]):
                        finished = True
                elif e > num_updates and not finished:
                    finished = True

    def update_lagrange_multipliers(self, e, max_lm=100.0):
        
        # Save relevant loss information for updating Lagrange parameters
        if self.k != None:
            if not utils.converged(self.recent_utilities[self.k]):
                if self.k != self.num_objectives - 1:
                    self.u[self.k] = torch.tensor(self.recent_utilities[self.k]).mean()
            else:
                self.k = 0 if self.k == self.num_objectives - 1 else self.k + 1
        else:
            for i in range(self.num_objectives - 1):
                self.u[i] = torch.tensor(self.recent_utilities[i]).mean()

        # Update Lagrange parameters
        r = self.k if self.k != None else self.num_objectives - 1
        for i in range(r):
            self.lagrange_multipliers[i] += self.lrs['lagrange_multiplier'][i](e) * (self.u[i] - self.recent_utilities[i][-1])
            self.lagrange_multipliers[i] = min(max(self.lagrange_multipliers[i], 0.0), max_lm)

    # Compute weights for lexicographic objectives
    def compute_weights(self, objective_range, e):

        objective_weights = []
        for k in range(objective_range - 1):
            if self.k != None:
                w = self.lrs['actors'][objective_range - 1] * self.lagrange_multipliers[k]
            else:
                w = self.lrs['actors'][k](e) + self.lagrange_multipliers[k] * sum([self.lrs['actors'][kk](e) for kk in range(k+1, objective_range)])
            objective_weights.append(w)
        objective_weights.append(self.lrs['actors'][objective_range - 1](e))

        return objective_weights

    # Output policy distribution at a particular state
    def policy(self, state, probs=False):

        action_dists = [actor(state) for actor in self.actors]
        if probs:
            return action_dists
        else:
            return [Categorical(a) for a in action_dists]

    # Return dict of policy dists
    def get_policy_dists(self, possible_states, possible_state_tensors):

        return dict([(p, self.policy(t, probs=True)) for p,t in zip(possible_states, possible_state_tensors)])

    # Perform (epsilon-greedy) joint action
    def act(self, state):

        if random.random() < self.hps['epsilon']:
            joint_action = [tt(random.choice(range(act_size))) for act_size in self.act_sizes]
        else:
            joint_action = [actions.sample() for actions in self.policy(state)]

        return joint_action

    # Take a learning step
    def step(self, info, objectives):

        # Reset Gamma terms if needed
        if info["t"] == 0:
            self.gammas = self.num_objectives * [1.0]

        # Form product states
        prod_state = torch.cat([info["f(s)"]] + info["f(q)"], 0)
        new_prod_state = torch.cat([info["f(s')"]] + info["f(q')"], 0)

        # Form scalar rewards and discounts
        if info["is_e_t"]:
            scalar_rewards = [0.0 for _ in info["R"]]
            scalar_discounts = [1.0 for _ in info["R"]]
        else:
            scalar_rewards = [r_f['reward'](info["s"], info["a"], info["s'"]) for r_f in info["R"]]
            scalar_discounts = [r_f['discount'] for r_f in info["R"]]

        rewards = [self.hps['spec_reward'] * f for f in info["F"]] + scalar_rewards
        discounts = [1 - f * (1 - self.hps['gamma_Phi']) for f in info["F"]] + scalar_discounts

        num_specs = len(info["F"])
        for j in range(self.num_objectives):

            # Temporarily save product state
            self.Z[j][tuple(prod_state.tolist())] = (info["t"], prod_state, info["a"])
        
            # Perform patient updates if possible
            add_stored_experiences = False
            if (j < num_specs and info["F"][j]):
                self.gammas[j] *= self.hps['gamma_Phi']
                # r = self.hps['spec_reward']
                add_stored_experiences = True
            elif (j >= num_specs and not info["is_e_t"]):
                self.gammas[j] *= discounts[j]
                # r = info["R"][j - self.num_specs](info["s"], info["a"], info["s'"])
                add_stored_experiences = True
            elif torch.allclose(self.critics[j](new_prod_state), tt(0.0), rtol=0.0, atol=1e-02):
                # r = 0.0
                add_stored_experiences = True
            elif info["done"]:
                add_stored_experiences = True
            elif not self.hps['patient_updates']:
                add_stored_experiences = True
         
            if add_stored_experiences:
                for k in self.Z[j].keys():
                    (t, s, a) = self.Z[j][k]
                    self.critic_buffers[j].add(t, s, a, rewards[j], new_prod_state, discounts[j], self.gammas[j], info["done"])
                self.Z[j] = dict()

        # Store (augmented) data for actors
        for q_1, q_2, f_s in zip(info["f(q)_s"], info["f(q')_s"], info["F_s"]):
            s_q_1 = torch.cat([info["f(s)"]] + q_1, 0)
            s_q_2 = torch.cat([info["f(s')"]] + q_2, 0)
            r = [self.hps['spec_reward'] * f for f in f_s] + scalar_rewards
            d_s = [1 - f * (1 - self.hps['gamma_Phi']) for f in f_s] + scalar_discounts
            self.actor_buffer.add(info["t"], s_q_1, info["a"], r, s_q_2, d_s, self.gammas, info["done"], prod_state)

            # Store augmented data for critics when possible
            if self.hps['augment_data']:
                # Avoid oversampling the true transition
                if torch.equal(s_q_1, prod_state):
                    continue
                # Add data for alternative transitions
                else:
                    for j in range(self.num_objectives):
                        if self.hps['patient_updates']:
                            add_experience = False
                            if (j < num_specs and f_s[j]):
                                r_j = self.hps['spec_reward']
                                add_experience = True
                            elif (j >= num_specs and not info["is_e_t"]):
                                r_j = rewards[j]
                                add_experience = True
                            elif torch.allclose(self.critics[j](s_q_2), tt(0.0), rtol=0.0, atol=1e-02):
                                r_j = 0.0
                                add_experience = True
                        else:
                            add_experience = True
                        if add_experience:
                            self.critic_buffers[j].add(info["t"], s_q_1, info["a"], r_j, s_q_2, d_s[j], self.gammas[j], info["done"])
                        
        # If enough data has been collected update the critics
        for j in range(self.num_objectives):
            if len(self.critic_buffers[j]) > self.hps['update_after']['critics']:
                data = self.critic_buffers[j].sample(sample_all=True)
                self.update_critic(j, data, until_converged=self.hps['until_converged']['critics'], num_updates=self.hps['num_updates']['critics'])
                self.critic_buffers[j].clear()
                self.updates['critics'][j] += 1
        
        # If enough data has been collected update the actors and the Lagrange multipliers
        if len(self.actor_buffer) > self.hps['update_after']['actors']:
            data = self.actor_buffer.sample(sample_all=True)
            self.update_actors(data, objectives, until_converged=self.hps['until_converged']['actors'], num_updates=self.hps['num_updates']['actors'])
            self.actor_buffer.clear()
            self.updates['actors'] += 1

        # return [r * d for r, d in zip(rewards, discounts)]

    def state_dist(self, ts, gammas):

        return power(self.hps['continue_prob'] * ones_like(ts), -ts) * gammas if self.hps['actual_dist'] else power(self.hps['continue_prob'] * ones_like(ts), -ts)

    def save_model(self, location, prefix):
        for i in range(self.num_players):
            torch.save(self.actors[i].state_dict(), '{}/models/{}-actor-{}.pt'.format(location, prefix, i))
        for j in range(self.num_objectives):
            torch.save(self.critics[j].state_dict(), '{}/models/{}-critic-{}.pt'.format(location, prefix, j))

    def load_model(self, root, prefix):
        for i in range(self.num_players):
            self.actors[i].load_state_dict(torch.load(('{}/models/{}-actor-{}.pt'.format(root, prefix, i))))
        for j in range(self.num_objectives):
            self.critics[j].load_state_dict(torch.load(('{}/models/{}-critic-{}.pt'.format(root, prefix, j))))


# RMAPPO module
class RMAPPO:

    def __init__(self, obs_size, act_sizes, objectives, hps, load_from=None):
        
        # TODO
        if load_from != None:
            pass
        else:
            pass

        self.name = 'rmappo'

        # Parameters
        self.act_sizes = act_sizes
        self.hps = hps
        self.lrs = set_up_lrs(self.hps['learning_rates'])
        self.objectives = objectives
        self.num_objectives = len(objectives)
        self.num_players = len(self.act_sizes)
        self.obs_size = obs_size

        # Values to maintain
        self.updates = 0
        self.gammas = self.num_objectives * [1.0]
        self.u = [0.0 for _ in range(self.num_objectives - 1)]
        self.k = 0 if self.hps['sequential'] else None
        self.recent_utilities = [deque(maxlen=25) for i in range(self.num_objectives)]
        self.kl_weight = 1.0
       
        # Networks
        shapes = self.hps['models']
        self.agents = [LSTM(self.obs_size, a_s, self.num_objectives, shapes['pre_hidden'], shapes['lstm'], shapes['act_hidden'], shapes['val_hidden']) for a_s in self.act_sizes]
        for agent in self.agents:
            agent.to(device)

        # Optimisers
        self.optimisers = set_up_optimisers(self.agents, self.hps['optimisers'], l2=self.hps['l2_weight'])

        # Buffers
        self.buffer = AgentBuffer(buffer_size=self.hps['buffers']['size'], batch_size=self.hps['buffers']['batch'])


    def update_agents(self, data, objectives, until_converged=True, num_updates=None):

        times, states, hx_cxs, joint_actions, rewards, next_states, next_hx_cxs, gammas, discounts, dones = data
        for i in range(self.num_players):
            self.agents[i].train()

        # Form state dists for weighting samples
        dists = [self.state_dist(times, gammas[j]).to(device) for j in range(self.num_objectives)]
        obj_dists = [dists[j] for j in objectives]
        
        # Form targets and useful quantities for updates
        with torch.no_grad():
            
            all_probs = []
            all_vals = []
            all_next_vals = []
            for agent in self.agents:
                probs, vals = agent(states, hx=hx_cxs)
                _, next_vals = agent(next_states, hx=next_hx_cxs)
                all_probs.append(probs)
                all_vals.append(vals)
                all_next_vals.append(next_vals)
            mean_vals = sum(all_vals)/self.num_players
            mean_next_vals = sum(all_next_vals)/self.num_players

            critic_targets = [rewards[j].to(device) + (discounts[j] * mean_next_vals[j] * (1-dones)).to(device) for j in range(self.num_objectives)]

            advantages = [critic_targets[j] - mean_vals[j] for j in objectives]
            if self.hps['normalise_advantages']:
                advs = [(a - a.mean()) / (a.std() + 1e-8) for a in advantages]
            else:
                advs = advantages

            old_log_probs = sum([torch.gather(torch.log(all_probs[i]), 1, joint_actions[i]) for i in range(self.num_players)])
        
        # Update networks
        finished = False
        temp_recent_losses = deque(maxlen=25)
        if num_updates == None:
            num_updates = len(times)
        e = 0
        while not finished:

            # Form weights
            objective_range = self.k + 1 if self.k != None else self.num_objectives
            objective_weights = self.compute_weights(objective_range, e)

            # Get useful quantities
            all_probs = []
            all_vals = []
            for agent in self.agents:
                probs, vals = agent(states, hx_cxs)
                all_probs.append(probs)
                all_vals.append(vals)
            mean_vals = sum(all_vals)/self.num_players
            new_log_probs = sum([torch.gather(torch.log(all_probs[i]), 1, joint_actions[i]) for i in range(self.num_players)])
            
            # Form losses for critic
            critic_loss = ((dists * (mean_vals - critic_targets)**2).mean()).to(device)

            # Form losses for actor
            log_ratio = new_log_probs - old_log_probs
            # log_ratio = (new_action_log_probs.masked_fill(new_action_log_probs == -np.inf, 0.0) - old_action_log_probs).to(device)
            ratio = torch.exp(log_ratio)
            kl_penalty = ratio * log_ratio

            if self.hps['entropy_weight'] != 0.0:
                entropy_penalty = sum([torch.unsqueeze((p * torch.log(p)).sum(dim=1), 1) for p in all_probs])
            else:
                entropy_penalty = 0.0

            obj_losses = [(obj_dists[k] * (ratio * advs[k] - self.kl_weight * kl_penalty + self.hps['entropy_weight'] * entropy_penalty)).mean() for k in range(self.num_objectives)]

            actor_loss = - sum([objective_weights[k] * obj_losses[k] for k in range(self.num_objectives)])

            # Backpropagate
            loss = critic_loss + actor_loss
            for i in range(self.num_players):
                self.optimisers[i].zero_grad()
            loss.backward()
            for i in range(self.num_players):
                self.optimisers[i].step()
                self.agents[i].eval()

            # Update KL weight term as in the original PPO paper
            mean_kl = kl_penalty.mean()
            if mean_kl < self.hps['kl_target'] / 1.5:
                self.kl_weight *= 0.5
            elif mean_kl > self.hps['kl_target'] * 1.5:
                self.kl_weight *= 2

            # Update Lagrange multipliers
            with torch.no_grad():
                for k in range(self.num_objectives):
                    self.recent_utilities[k].append(obj_losses[k])
            self.update_lagrange_multipliers(e)

            # Check whether to finish updating
            e += 1
            temp_recent_losses.append(loss)
            if until_converged:
                if self.k == None:
                    finished = utils.converged(temp_recent_losses)
                elif self.k == self.num_objectives - 1 and utils.converged(self.recent_utilities[self.k]):
                    finished = True
            elif e > num_updates:
                finished = True


    def update_lagrange_multipliers(self, e, max_lm=100.0):
        
        # Save relevant loss information for updating Lagrange parameters
        if self.k != None:
            if not utils.converged(self.recent_utilities[self.k]):
                if self.k != self.num_objectives - 1:
                    self.u[self.k] = torch.tensor(self.recent_utilities[self.k]).mean()
            else:
                self.k = 0 if self.k == self.num_objectives - 1 else self.k + 1
        else:
            for i in range(self.num_objectives - 1):
                self.u[i] = torch.tensor(self.recent_utilities[i]).mean()

        # Update Lagrange parameters
        r = self.k if self.k != None else self.num_objectives - 1
        for i in range(r):
            self.lagrange_multipliers[i] += self.lrs['lagrange_multiplier'][i](e) * (self.u[i] - self.recent_utilities[i][-1])
            self.lagrange_multipliers[i] = min(max(self.lagrange_multipliers[i], 0.0), max_lm)

    # Compute weights for lexicographic objectives
    def compute_weights(self, objective_range, e):

        objective_weights = []
        for k in range(objective_range - 1):
            if self.k != None:
                w = self.lrs['actors'][objective_range - 1] * self.lagrange_multipliers[k]
            else:
                w = self.lrs['actors'][k](e) + self.lagrange_multipliers[k] * sum([self.lrs['actors'][kk](e) for kk in range(k+1, objective_range)])
            objective_weights.append(w)
        objective_weights.append(self.lrs['actors'][objective_range - 1](e))

        return objective_weights

    # Perform (epsilon-greedy) joint action
    def act(self, state):

        if random.random() < self.hps['epsilon']:
            joint_action = [tt(random.choice(range(act_size))) for act_size in self.act_sizes]
        else:
            joint_action = [Categorical(agent(state)[0]).sample() for agent in self.agents]

        return joint_action

    # Reset hidden states
    def reset(self):

        for agent in self.agents:
            agent.reset()

    def get_hidden_states(self):

        return [agent.get_hidden_state() for agent in self.agents]

    # Take a learning step
    def step(self, info, objectives):

        num_specs = len(info["F"][0])

        # Reset Gamma terms if needed
        if info["t"] == 0:
            self.gammas = self.num_objectives * [1.0]

        # Form product states
        state = info["f(s)"]
        next_state = info["f(s')"]

        # Form scalar rewards and discounts
        scalar_rewards = [r_f['reward'](info["s"], info["a"], info["s'"]) for r_f in info["R"]]
        scalar_discounts = [r_f['discount'] for r_f in info["R"]]

        # Form all rewards and discounts
        rewards = [self.hps['spec_reward'] * f for f in info["F"]] + scalar_rewards
        discounts = [1 - f * (1 - self.hps['gamma_Phi']) for f in info["F"]] + scalar_discounts

        # Update gammas
        for j in range(self.num_objectives):
            if (j < num_specs and info["F"][j]):
                self.gammas[j] *= self.hps['gamma_Phi']
                add_stored_experiences = True
            elif (j >= num_specs and not info["is_e_t"]):
                self.gammas[j] *= discounts[j]

        # Add datum
        self.buffer.add(time=info["t"], state=state, hx_cx=info["h"], joint_action=info["a"], reward=rewards, next_state=next_state, next_hx_cx=info["h'"], gamma=self.gammas, discount=discounts, done=info["done"])

        # If enough data has been collected update the agents and the Lagrange multipliers
        if len(self.buffer) > self.hps['update_after']:
            data = self.buffer.sample(sample_all=True)
            self.update_agents(data, objectives, until_converged=self.hps['until_converged'], num_updates=self.hps['num_updates'])
            self.buffer.clear()
            self.updates += 1

    def state_dist(self, ts, gammas):

        return power(self.hps['continue_prob'] * ones_like(ts), -ts) * gammas if self.hps['actual_dist'] else power(self.hps['continue_prob'] * ones_like(ts), -ts)

    def save_model(self, location, prefix):
        for i in range(self.num_players):
            torch.save(self.agents[i].state_dict(), '{}/models/{}-agent-{}.pt'.format(location, prefix, i))

    def load_model(self, root, prefix):
        for i in range(self.num_players):
            self.agents[i].load_state_dict(torch.load(('{}/models/{}-agent-{}.pt'.format(root, prefix, i))))