import argparse
import os
from copy import deepcopy
import torch
from utils.save_load import mkdir_save, load
from utils.functional import disp_num_params
from timeit import default_timer as timer
from tqdm import tqdm
from abc import ABC, abstractmethod


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-d', '--density',
                        help="Remaining model density",
                        action='store',
                        dest='density',
                        type=float,
                        default=0,
                        required=False)
    parser.add_argument('-c', '--client-selection',
                        help="If use client-selection",
                        action='store_true',
                        dest='client_selection',
                        default=False,
                        required=False)
    parser.add_argument('-s', '--seed',
                        help="The seed to use for the prototype",
                        action='store',
                        dest='seed',
                        type=int,
                        default=0,
                        required=False)
    parser.add_argument('-e', '--exp-name',
                        help="Experiment name",
                        action='store',
                        dest='experiment_name',
                        type=str,
                        required=True)

    return parser.parse_args()


class SnipServer(ABC):
    def __init__(self, args, config, model, save_interval=50):
        self.config = config
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.experiment_name = args.experiment_name
        self.save_path = os.path.join("results", config.EXP_NAME, args.experiment_name)
        self.save_interval = save_interval

        adaptive_folder = "adaptive_cs" if args.client_selection else "adaptive_them_all_15"   #navigate to optimum
        if args.density == 0:
            # self.density = load("results/{}/{}/model.pt".format(config.EXP_NAME, adaptive_folder)).density()
            self.density = 0.6570325174274417
        else:
            self.density = args.density

        self.model = model.to(self.device)
        self.model.train()

        mkdir_save(self.model, os.path.join(self.save_path, "init_model.pt"))

        self.test_loader = None

        self.init_test_loader()
        self.init_clients()

    @abstractmethod
    def init_test_loader(self):
        pass

    @abstractmethod
    def init_clients(self):
        pass

    def main(self, idx, list_sd, list_num_proc, lr, start, list_loss, list_acc, list_est_time, list_model_size):
        total_num_proc = sum(list_num_proc)

        grad_dict = dict()
        weight_dict = dict()
        with torch.no_grad():
            for key, param in self.model.state_dict().items():
                avg_inc_val = None
                for num_proc, state_dict in zip(list_num_proc, list_sd):
                    if key in state_dict.keys():
                        inc_val = state_dict[key] - param

                        if avg_inc_val is None:
                            avg_inc_val = num_proc / total_num_proc * inc_val
                        else:
                            avg_inc_val += num_proc / total_num_proc * inc_val

                if avg_inc_val is None or key.endswith("num_batches_tracked"):
                    continue
                else:
                    if idx == 0 and key in dict(self.model.named_parameters()).keys() and key.endswith(
                            "weight") and key[:-7] in self.model.prunable_layer_prefixes:
                        grad_dict[key] = avg_inc_val / lr
                        weight_dict[key] = dict(self.model.named_parameters())[key].clone()
                    param.add_(avg_inc_val)

        if idx == 0:
            abs_all_wg = None
            for (name_w, w), (name_g, g) in zip(weight_dict.items(), grad_dict.items()):
                assert name_w == name_g
                if abs_all_wg is None:
                    abs_all_wg = (w * g).view(-1).abs()
                else:
                    abs_all_wg = torch.cat([abs_all_wg, (w * g).view(-1).abs()], dim=0)

            threshold = abs_all_wg.sort(descending=True)[0][int(self.density * abs_all_wg.nelement())]
            for layer, layer_prefix in zip(self.model.prunable_layers, self.model.prunable_layer_prefixes):
                abs_layer_wg = (weight_dict[layer_prefix + ".weight"] * grad_dict[layer_prefix + ".weight"]).abs()
                layer.mask = abs_layer_wg >= threshold

            with torch.no_grad():
                for layer in self.model.prunable_layers:
                    layer.weight *= layer.mask

            print("Snip pruning completed. Remaining params:")
            disp_num_params(self.model)

        if idx % self.config.EVAL_DISP_INTERVAL == 0:
            loss, acc = self.model.evaluate(self.test_loader)
            list_loss.append(loss)
            list_acc.append(acc)

            print("Round #{} (Experiment = {}).".format(idx, self.experiment_name))
            print("Loss/acc (at round {}) = {}/{}".format((len(list_loss) - 1) * self.config.EVAL_DISP_INTERVAL, loss,
                                                          acc))
            print("Estimated time = {}".format(sum(list_est_time)))
            print("Elapsed time = {}".format(timer() - start))

        est_time = self.config.TIME_CONSTANT
        for layer, comp_coeff in zip(self.model.prunable_layers, self.config.COMP_COEFFICIENTS):
            est_time += layer.num_weight * (comp_coeff + self.config.COMM_COEFFICIENT)

        model_size = self.model.calc_num_all_active_params(True)
        list_est_time.append(est_time)
        list_model_size.append(model_size)

        if idx % self.save_interval == 0:
            mkdir_save(list_loss, os.path.join(self.save_path, "loss.pt"))
            mkdir_save(list_acc, os.path.join(self.save_path, "accuracy.pt"))
            mkdir_save(list_est_time, os.path.join(self.save_path, "est_time.pt"))
            mkdir_save(list_model_size, os.path.join(self.save_path, "model_size.pt"))
            mkdir_save(self.model, os.path.join(self.save_path, "model.pt"))

        return [layer.mask for layer in self.model.prunable_layers], [self.model.state_dict() for _ in
                                                                      range(self.config.NUM_CLIENTS)]


class SnipClient:
    def __init__(self, config, model):
        self.config = config
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.model = deepcopy(model).to(self.device)

        self.optimizer = None
        self.optimizer_scheduler = None
        self.optimizer_wrapper = None
        self.train_loader = None

    @abstractmethod
    def init_optimizer(self, *arg, **kwargs):
        pass

    @abstractmethod
    def init_train_loader(self, *arg, **kwargs):
        pass

    def main(self):
        self.model.train()
        num_proc_data = 0

        lr = self.optimizer_wrapper.get_last_lr()

        # for _ in range(self.config.NUM_LOCAL_UPDATES):
        #     inputs, labels = self.train_loader.get_next_batch()
        #     self.optimizer_wrapper.step(inputs.to(self.device), labels.to(self.device))

        #     num_proc_data += len(inputs)

        # self.optimizer_wrapper.lr_scheduler_step()
        for train_batch1, train_batch2,train_batch3, train_labels in tqdm(self.train_loader):
            train_batch1, train_batch2,train_batch3,train_labels = train_batch1.float().to(self.device), train_batch2.float().to(self.device),train_batch3.float().to(self.device),train_labels.float().to(self.device)
            self.optimizer_wrapper.step(train_batch1,train_batch2,train_batch3, train_labels)
            num_proc_data += len(train_batch1)    # not important
        self.optimizer_wrapper.lr_scheduler_step()

        return self.model.state_dict(), num_proc_data, lr

    @torch.no_grad()
    def load_mask(self, idx, masks):
        assert idx == 0
        for layer, new_mask in zip(self.model.prunable_layers, masks):
            layer.mask = new_mask.clone().to(layer.mask.device)
            layer.weight *= layer.mask

        self.optimizer.clear_state()

    def load_state_dict(self, state_dict):
        self.model.load_state_dict(state_dict)


class SnipFL:
    def __init__(self, config, server, client_list):
        self.max_round = config.MAX_ROUND
        self.server = server
        self.client_list = client_list
        self.list_loss, self.list_acc, self.list_est_time, self.list_model_size = [], [], [], []

    def main(self):
        start = timer()
        for idx in range(self.max_round):
            list_state_dict, list_num, list_last_lr = [], [], []

            for client in self.client_list:
                sd, npc, last_lr = client.main()
                list_state_dict.append(sd)
                list_num.append(npc)
                list_last_lr.append(last_lr)
            last_lr = list_last_lr[0]
            for client_lr in list_last_lr[1:]:
                assert client_lr == last_lr

            list_mask, new_list_sd = self.server.main(idx, list_state_dict, list_num, last_lr, start, self.list_loss,
                                                      self.list_acc, self.list_est_time, self.list_model_size)
            for client, new_sd in zip(self.client_list, new_list_sd):
                client.load_state_dict(new_sd)
                if idx == 0:
                    client.load_mask(idx, list_mask)
