import random
import copy
import torch
import torch.nn as nn
import torch.nn.functional as Fnn
from torch.autograd import Variable

from utils import gae, cuda_if


class PPO:
    def __init__(self, policy, venv, optimizer, clip=.1, gamma=.99, lambd=.95,
                 worker_steps=128, sequence_steps=32, batch_steps=256,
                 opt_epochs=3, value_coef=1., entropy_coef=.01, max_grad_norm=.5,
                 cuda=False):
        """ Proximal Policy Optimization algorithm class

        Evaluates a policy over a vectorized environment and
        optimizes over policy, value, entropy objectives.

        Assumes discrete action space.

        Args:
            policy (nn.Module): the policy to optimize
            venv (vec_env): the vectorized environment to use
            optimizer (optim.Optimizer): the optimizer to use
            clip (float): probability ratio clipping range
            gamma (float): discount factor
            lambd (float): GAE lambda parameter
            worker_steps (int): steps per worker between optimization rounds
            sequence_steps (int): steps per sequence (for backprop through time)
            batch_steps (int): steps per sequence (for backprop through time)
        """
        self.policy = policy
        self.policy_old = copy.deepcopy(policy)
        self.venv = venv
        self.optimizer = optimizer

        self.num_workers = venv.num_envs
        self.worker_steps = worker_steps
        self.sequence_steps = sequence_steps
        self.batch_steps = batch_steps

        self.opt_epochs = opt_epochs
        self.gamma = gamma
        self.lambd = lambd
        self.value_coef = value_coef
        self.entropy_coef = entropy_coef
        self.max_grad_norm = max_grad_norm
        self.cuda = cuda

        self.objective = PPOObjective(clip)

        self.last_ob = self.venv.reset()

    def run(self, total_steps):
        """ Runs PPO

        Args:
            total_steps (int): total number of environment steps to run for
        """
        taken_steps = 0

        N = self.num_workers
        T = self.worker_steps
        E = self.opt_epochs
        A = self.venv.action_space.n

        while taken_steps < total_steps:
            obs, rewards, masks, actions, steps = self.interact()
            ob_shape = obs.size()[2:]

            # compute advantages, returns with GAE
            # TEMP upgrade to support recurrence
            obs_ = obs.view(((T + 1) * N,) + ob_shape)
            obs_ = Variable(obs_)
            _, values = self.policy(obs_)
            values = values.view(T + 1, N, 1)
            advantages, returns = gae(rewards, masks, values, self.gamma, self.lambd)

            self.policy_old.load_state_dict(self.policy.state_dict())
            for e in range(E):
                self.policy.zero_grad()

                S = self.sequence_steps
                B_total = steps // self.sequence_steps
                B = self.batch_steps // self.sequence_steps

                batch = random.sample(range(B_total), B)

                b_obs = Variable(obs[:T].view((S, B_total) + ob_shape)[:, batch])
                b_rewards = Variable(rewards.view(S, B_total, 1)[:, batch])
                b_masks = Variable(masks.view(S, B_total, 1)[:, batch])
                b_actions = Variable(actions.view(S, B_total, 1)[:, batch])
                b_advantages = Variable(advantages.view(S, B_total, 1)[:, batch])
                b_returns = Variable(returns.view(S, B_total, 1)[:, batch])

                # eval policy and old policy
                b_pis, b_vs, b_pi_olds, b_v_olds = [], [], [], []
                for s in range(S):
                    pi, v = self.policy(b_obs[s])
                    b_pis.append(pi)
                    b_vs.append(v)
                    pi_old, v_old = self.policy_old(b_obs[s])
                    b_pi_olds.append(pi_old)
                    b_v_olds.append(v_old)
                cat_fn = lambda l: torch.cat([e.unsqueeze(0) for e in l])
                condensed = [cat_fn(item) for item in [b_pis, b_vs, b_pi_olds, b_v_olds]]
                b_pis, b_vs, b_pi_olds, b_v_olds = tuple(condensed)

                losses = self.objective(b_pis.view(S * B, A),
                                        b_vs.view(S * B, 1),
                                        b_pi_olds.view(S * B, A).detach(),
                                        b_v_olds.view(S * B, 1).detach(),
                                        b_actions.view(S * B, 1),
                                        b_advantages.view(S * B, 1),
                                        b_returns.view(S * B, 1))
                policy_loss, value_loss, entropy_loss = losses

                loss = policy_loss + value_loss * self.value_coef + entropy_loss * self.entropy_coef

                loss.backward()
                torch.nn.utils.clip_grad_norm(self.policy.parameters(), self.max_grad_norm)
                self.optimizer.step()

            taken_steps += steps
            print(taken_steps)

    def interact(self):
        """ Interacts with the environment

        Returns:
            obs (ArgumentDefaultsHelpFormatternsor): observations shaped [T + 1 x N x ...]
            rewards (FloatTensor): rewards shaped [T x N x 1]
            masks (FloatTensor): continuation masks shaped [T x N x 1]
                zero at done timesteps, one otherwise
            actions (LongTensor): discrete actions shaped [T x N x 1]
            steps (int): total number of steps taken
        """
        N = self.num_workers
        T = self.worker_steps

        # TEMP needs to be generalized, does conv-specific transpose for PyTorch
        obs = torch.zeros(T + 1, N, 4, 84, 84)
        obs = cuda_if(obs, self.cuda)
        rewards = torch.zeros(T, N, 1)
        rewards = cuda_if(rewards, self.cuda)
        masks = torch.zeros(T, N, 1)
        masks = cuda_if(masks, self.cuda)
        actions = torch.zeros(T, N, 1).long()
        actions = cuda_if(actions, self.cuda)

        for t in range(T):
            ob = torch.from_numpy(self.last_ob.transpose((0, 3, 1, 2))).float()
            ob = Variable(ob / 255.)
            ob = cuda_if(ob, self.cuda)
            obs[t] = ob.data

            pi, v = self.policy(ob)
            prob = Fnn.softmax(pi)
            action = prob.multinomial().data
            actions[t] = action

            self.last_ob, reward, done, _ = self.venv.step(action.cpu().numpy())
            rewards[t] = torch.from_numpy(reward).unsqueeze(1)
            mask = torch.from_numpy((1. - done)).unsqueeze(1)
            masks[t] = mask

        ob = torch.from_numpy(self.last_ob.transpose((0, 3, 1, 2))).float()
        ob = Variable(ob / 255.)
        ob = cuda_if(ob, self.cuda)
        obs[T] = ob.data

        steps = N * T

        return obs, rewards, masks, actions, steps

class PPOObjective(nn.Module):
    def __init__(self, clip):
        super().__init__()

        self.clip = clip

    def forward(self, pi, v, pi_old, v_old, action, advantage, returns):
        """ Computes PPO objectives

        Assumes discrete action space.

        Args:
            pi (Variable): discrete action logits, shaped [N x num_actions]
            v (Variable): value predictions, shaped [N x 1]
            pi_old (Variable): old discrete action logits, shaped [N x num_actions]
            v_old (Variable): old value predictions, shaped [N x 1]
            action (Variable): discrete actions, shaped [N x 1]
            advantage (Variable): action advantages, shaped [N x 1]
            returns (Variable): discounted returns, shaped [N x 1]

        Returns:
            policy_loss (Variable): policy surrogate loss, shaped [1]
            value_loss (Variable): value loss, shaped [1]
            entropy_loss (Variable): entropy loss, shaped [1]
        """
        prob = Fnn.softmax(pi)
        log_prob = Fnn.log_softmax(pi)
        action_prob = prob.gather(1, action)

        prob_old = Fnn.softmax(pi_old)
        action_prob_old = prob_old.gather(1, action)

        ratio = action_prob / (action_prob_old + 1e-10)

        advantage = (advantage - advantage.mean()) / (advantage.std() + 1e-5)

        surr1 = ratio * advantage
        surr2 = torch.clamp(ratio, min=1. - self.clip, max=1. + self.clip) * advantage

        policy_loss = -torch.min(surr1, surr2).mean()
        value_loss = (.5 * (v - returns) ** 2.).mean()
        entropy_loss = (prob * log_prob).sum(1).mean()

        return policy_loss, value_loss, entropy_loss
