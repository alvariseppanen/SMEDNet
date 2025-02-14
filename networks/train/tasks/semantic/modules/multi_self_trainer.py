#!/usr/bin/env python3
# This file is covered by the LICENSE file in the root of this project.
from cmath import nan
import datetime
from logging.config import valid_ident
import os
import time
import imp
import cv2
import torch
import torch.backends.cudnn as cudnn
import torch.nn as nn
import torch.optim as optim
from matplotlib import pyplot as plt
from torch.autograd import Variable
from common.avgmeter import *
from common.logger import Logger
from common.sync_batchnorm.batchnorm import convert_model
from common.warmupLR import *
from tasks.semantic.modules.ioueval import *
from tasks.semantic.modules.CoordinateL import *
from tasks.semantic.modules.CorrelationL import *
from tasks.semantic.modules.KNN_search import KNN_search
import random
import sys

#torch.use_deterministic_algorithms(True)

# for reproducibility
os.environ["CUBLAS_WORKSPACE_CONFIG"]=":4096:2" # for torch.matmul to be deterministic
torch.manual_seed(1234)
torch.backends.cudnn.deterministic = True
random.seed(1234)
np.random.seed(1234)

def keep_variance_fn(x):
    return x + 1e-3

def one_hot_pred_from_label(y_pred, labels):
    y_true = torch.zeros_like(y_pred)
    ones = torch.ones_like(y_pred)
    indexes = [l for l in labels]
    y_true[torch.arange(labels.size(0)), indexes] = ones[torch.arange(labels.size(0)), indexes]

    return y_true


def save_to_log(logdir, logfile, message):
    f = open(logdir + '/' + logfile, "a")
    f.write(message + '\n')
    f.close()
    return


def save_checkpoint(to_save, logdir, suffix=""):
    # Save the weights
    torch.save(to_save, logdir +
               "/SMEDNet" + suffix)


class Trainer():
    def __init__(self, ARCH, DATA, datadir, logdir, path=None):
        # parameters
        self.ARCH = ARCH
        self.DATA = DATA
        self.datadir = datadir
        self.log = logdir
        self.path = path

        self.batch_time_t = AverageMeter()
        self.data_time_t = AverageMeter()
        self.batch_time_e = AverageMeter()
        self.epoch = 0

        self.info = {"train_update": 0,
                     "train_loss": 0,
                     "train_acc": 0,
                     "train_iou": 0,
                     "valid_loss": 0,
                     "valid_acc": 0,
                     "valid_iou": 0,
                     "best_train_iou": 0,
                     "best_val_iou": 0}

        # get the data
        parserModule = imp.load_source("parserModule",
                                       booger.TRAIN_PATH + '/tasks/semantic/dataset/' +
                                       self.DATA["name"] + '/multi_parser.py')
        self.parser = parserModule.Parser(root=self.datadir,
                                          train_sequences=self.DATA["split"]["train"],
                                          valid_sequences=self.DATA["split"]["valid"],
                                          test_sequences=None,
                                          split='train',
                                          labels=self.DATA["labels"],
                                          color_map=self.DATA["color_map"],
                                          learning_map=self.DATA["learning_map"],
                                          learning_map_inv=self.DATA["learning_map_inv"],
                                          sensor=self.ARCH["dataset"]["sensor"],
                                          max_points=self.ARCH["dataset"]["max_points"],
                                          batch_size=self.ARCH["train"]["batch_size"],
                                          workers=self.ARCH["train"]["workers"],
                                          gt=False,
                                          shuffle_train=True)

        self.n_echoes = self.ARCH["dataset"]["sensor"]["n_echoes"]

        # weights for loss (and bias)
        epsilon_w = self.ARCH["train"]["epsilon_w"]
        content = torch.zeros(self.parser.get_n_classes(), dtype=torch.float)
        for cl, freq in DATA["content"].items():
            x_cl = self.parser.to_xentropy(cl)  # map actual class to xentropy class
            content[x_cl] += freq
        self.loss_w = 1 / (content + epsilon_w)  # get weights
        for x_cl, w in enumerate(self.loss_w):  # ignore the ones necessary to ignore
            if DATA["learning_ignore"][x_cl]:
                # don't weigh
                self.loss_w[x_cl] = 0
        print("Loss weights from content: ", self.loss_w.data)

        with torch.no_grad():
            self.model = CorrL(self.parser.get_n_classes(), self.ARCH, n_echoes=self.n_echoes)
            self.model2 = CoorL(self.parser.get_n_classes(), self.ARCH, n_echoes=self.n_echoes)

        self.tb_logger = Logger(self.log + "/tb") 
        self.knn_search = KNN_search()

        # GPU?
        self.gpu = False
        self.multi_gpu = False
        self.n_gpus = 0
        self.model_single = self.model
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        print("Training in device: ", self.device)
        if torch.cuda.is_available() and torch.cuda.device_count() > 0:
            cudnn.benchmark = True
            cudnn.fastest = True
            self.gpu = True
            self.n_gpus = 1
            self.model.cuda()
            self.model2.cuda()
        if torch.cuda.is_available() and torch.cuda.device_count() > 1:
            print("Let's use", torch.cuda.device_count(), "GPUs!")
            self.model = nn.DataParallel(self.model)  # spread in gpus
            self.model = convert_model(self.model).cuda()  # sync batchnorm
            self.model_single = self.model.module  # single model to get weight names
            self.multi_gpu = True
            self.n_gpus = torch.cuda.device_count()

        
        self.optimizer = optim.SGD([{'params': self.model.parameters()}],
                                   lr=self.ARCH["train"]["lr"],
                                   momentum=self.ARCH["train"]["momentum"],
                                   weight_decay=self.ARCH["train"]["w_decay"])
        
        self.optimizer2 = optim.SGD([{'params': self.model2.parameters()}],
                                   lr=self.ARCH["train"]["tlr"],
                                   momentum=self.ARCH["train"]["momentum"],
                                   weight_decay=self.ARCH["train"]["w_decay"])


        # Use warmup learning rate
        # post decay and step sizes come in epochs and we want it in steps
        steps_per_epoch = self.parser.get_train_size()
        up_steps = int(self.ARCH["train"]["wup_epochs"] * steps_per_epoch)
        final_decay = self.ARCH["train"]["lr_decay"] ** (1 / steps_per_epoch)
        self.scheduler = warmupLR(optimizer=self.optimizer,
                                  lr=self.ARCH["train"]["lr"],
                                  warmup_steps=up_steps,
                                  momentum=self.ARCH["train"]["momentum"],
                                  decay=final_decay)

        self.scheduler2 = warmupLR(optimizer=self.optimizer2,
                                  lr=self.ARCH["train"]["tlr"],
                                  warmup_steps=up_steps,
                                  momentum=self.ARCH["train"]["momentum"],
                                  decay=final_decay)        

        if self.path is not None:
            torch.nn.Module.dump_patches = True
            w_dict = torch.load(path + "/SMEDNet",
                                map_location=lambda storage, loc: storage)
            self.model.load_state_dict(w_dict['state_dict'], strict=True)
            self.model2.load_state_dict(w_dict['state_dict_teacher'], strict=True)
            self.optimizer.load_state_dict(w_dict['optimizer'])
            self.optimizer2.load_state_dict(w_dict['optimizer2'])
            self.epoch = w_dict['epoch'] + 1
            self.scheduler.load_state_dict(w_dict['scheduler'])
            self.scheduler2.load_state_dict(w_dict['scheduler2'])
            print("dict epoch:", w_dict['epoch'])
            self.info = w_dict['info']
            print("info", w_dict['info'])

    def calculate_estimate(self, epoch, iter):
        estimate = int((self.data_time_t.avg + self.batch_time_t.avg) * \
                       (self.parser.get_train_size() * self.ARCH['train']['max_epochs'] - (
                               iter + 1 + epoch * self.parser.get_train_size()))) + \
                   int(self.batch_time_e.avg * self.parser.get_valid_size() * (
                           self.ARCH['train']['max_epochs'] - (epoch)))
        return str(datetime.timedelta(seconds=estimate))

    @staticmethod
    def get_mpl_colormap(cmap_name):
        cmap = plt.get_cmap(cmap_name)
        # Initialize the matplotlib color map
        sm = plt.cm.ScalarMappable(cmap=cmap)
        # Obtain linear color range
        color_range = sm.to_rgba(np.linspace(0, 1, 256), bytes=True)[:, 2::-1]
        return color_range.reshape(256, 1, 3)

    @staticmethod
    def make_log_img(depth, mask, pred, gt, color_fn):
        # input should be [depth, pred, gt]
        # make range image (normalized to 0,1 for saving)
        depth = (cv2.normalize(depth, None, alpha=0, beta=1,
                               norm_type=cv2.NORM_MINMAX,
                               dtype=cv2.CV_32F) * 255.0).astype(np.uint8)
        out_img = cv2.applyColorMap(
            depth, Trainer.get_mpl_colormap('viridis')) * mask[..., None]
        # make label prediction
        pred_color = color_fn((pred * mask).astype(np.int32))
        out_img = np.concatenate([out_img, pred_color], axis=0)
        # make label gt
        gt_color = color_fn(gt)
        out_img = np.concatenate([out_img, gt_color], axis=0)
        return (out_img).astype(np.uint8)

    @staticmethod
    def save_to_log(logdir, logger, info, epoch, w_summary=False, model=None, img_summary=False, imgs=[]):
        # save scalars
        for tag, value in info.items():
            if 'valid_classes' in tag:
                # solve the bug of saving tensor type of value
                continue
            logger.scalar_summary(tag, value, epoch)

        # save summaries of weights and biases
        if w_summary and model:
            for tag, value in model.named_parameters():
                tag = tag.replace('.', '/')
                logger.histo_summary(tag, value.data.cpu().numpy(), epoch)
                if value.grad is not None:
                    logger.histo_summary(
                        tag + '/grad', value.grad.data.cpu().numpy(), epoch)

        if img_summary and len(imgs) > 0:
            directory = os.path.join(logdir, "predictions")
            if not os.path.isdir(directory):
                os.makedirs(directory)
            for i, img in enumerate(imgs):
                name = os.path.join(directory, str(i) + ".png")
                cv2.imwrite(name, img)

    def train(self):

        self.ignore_class = []
        for i, w in enumerate(self.loss_w):
            if w < 1e-10:
                self.ignore_class.append(i)
                print("Ignoring class ", i, " in IoU evaluation")
        self.evaluator = iouEval(self.parser.get_n_classes(),
                                 self.device, self.ignore_class)

        # train for n epochs
        for epoch in range(self.epoch, self.ARCH["train"]["max_epochs"]):

            # train for 1 epoch
            acc, iou, loss, update_mean,hetero_l = self.train_epoch(train_loader=self.parser.get_train_set(),
                                                           model=self.model,
                                                           model2 = self.model2,
                                                           optimizer=self.optimizer,
                                                           optimizer2=self.optimizer2,
                                                           epoch=epoch,
                                                           evaluator=self.evaluator,
                                                           scheduler=self.scheduler,
                                                           scheduler2=self.scheduler2,
                                                           color_fn=self.parser.to_color,
                                                           report=self.ARCH["train"]["report_batch"],
                                                           show_scans=self.ARCH["train"]["show_scans"])

            # update info
            self.info["train_update"] = update_mean
            self.info["train_loss"] = loss
            self.info["train_acc"] = acc
            self.info["train_iou"] = iou
            self.info["train_hetero"] = hetero_l

            # remember best iou and save checkpoint
            state = {'epoch': epoch, 'state_dict': self.model.state_dict(),
                     'state_dict_teacher': self.model2.state_dict(),
                     'optimizer': self.optimizer.state_dict(),
                     'optimizer2': self.optimizer2.state_dict(),
                     'info': self.info,
                     'scheduler': self.scheduler.state_dict(),
                     'scheduler2': self.scheduler2.state_dict()
                     }
            save_checkpoint(state, self.log, suffix=""+str(epoch))

            if self.info['train_iou'] > self.info['best_train_iou']:
                print("Best mean iou in training set so far, save model!")
                self.info['best_train_iou'] = self.info['train_iou']
                state = {'epoch': epoch, 'state_dict': self.model.state_dict(),
                         'state_dict_teacher': self.model2.state_dict(),
                         'optimizer': self.optimizer.state_dict(),
                         'optimizer2': self.optimizer2.state_dict(),
                         'info': self.info,
                         'scheduler': self.scheduler.state_dict(),
                         'scheduler2': self.scheduler2.state_dict()
                         }
                save_checkpoint(state, self.log, suffix="_train_best")

            if epoch % self.ARCH["train"]["report_epoch"] and self.n_echoes == 1:
                # evaluate on validation set
                print("*" * 80)
                acc, iou, loss, rand_img,hetero_l = self.validate(val_loader=self.parser.get_valid_set(),
                                                         model=self.model,
                                                         model2=self.model2,
                                                         evaluator=self.evaluator,
                                                         class_func=self.parser.get_xentropy_class_string,
                                                         color_fn=self.parser.to_color,
                                                         save_scans=self.ARCH["train"]["save_scans"])

                # update info
                self.info["valid_loss"] = loss
                self.info["valid_acc"] = acc
                self.info["valid_iou"] = iou
                self.info['valid_heteros'] = hetero_l

            # remember best iou and save checkpoint
            if self.info['valid_iou'] > self.info['best_val_iou']:
                print("Best mean iou in validation so far, save model!")
                print("*" * 80)
                self.info['best_val_iou'] = self.info['valid_iou']

                # save the weights!
                state = {'epoch': epoch, 'state_dict': self.model.state_dict(),
                         'state_dict_teacher': self.model2.state_dict(),
                         'optimizer': self.optimizer.state_dict(),
                         'optimizer2': self.optimizer2.state_dict(),
                         'info': self.info,
                         'scheduler': self.scheduler.state_dict(),
                         'scheduler2': self.scheduler2.state_dict()
                         }
                save_checkpoint(state, self.log, suffix="_valid_best")

            print("*" * 80)

            # save to log 
            '''Trainer.save_to_log(logdir=self.log,
                                logger=self.tb_logger,
                                info=self.info,
                                epoch=epoch,
                                w_summary=self.ARCH["train"]["save_summary"],
                                model=self.model_single,
                                img_summary=self.ARCH["train"]["save_scans"],
                                imgs=rand_img)'''

        print('Finished Training')

        return

    def train_epoch(self, train_loader, model, model2, optimizer, optimizer2, epoch, evaluator, scheduler, scheduler2, color_fn, report=10,
                    show_scans=False):
        losses = AverageMeter()
        acc = AverageMeter()
        iou = AverageMeter()
        hetero_l = AverageMeter()
        update_ratio_meter = AverageMeter()

        # empty the cache to train now
        if self.gpu:
            torch.cuda.empty_cache()

        # switch to train mode
        model.train()
        model2.train()

        end = time.time()
        for i, (in_vol, proj_mask, proj_labels, _, _, _, _, _, _, proj_range, _, _, _, _, npoints, stack_order) in enumerate(train_loader):
            # measure data loading time
            self.data_time_t.update(time.time() - end)
            if not self.multi_gpu and self.gpu:
                in_vol = in_vol.cuda()

            B, C, H, W = in_vol.shape[0], int(in_vol.shape[1]/self.n_echoes), in_vol.shape[-2], in_vol.shape[-1]
            
            valid_mask = (in_vol[:, 0:self.n_echoes, ...] > 0).int()
            binary_mask = torch.bernoulli(torch.full((B, 1, H, W), 0.5)).bool() # 1 for trainable pixels
            if self.gpu: binary_mask = binary_mask.cuda()

            proj_range = torch.clamp(in_vol[:, 0:self.n_echoes, ...].detach(), min=1.0, max=80.0)

            intensity = (in_vol[:, (C-1)*self.n_echoes:(C-1)*self.n_echoes+self.n_echoes, ...].detach()) * proj_range**2 # normalized with inverse square law

            # run NNs
            p_difficulty = model(in_vol)
            p_range, knn_values = model2(in_vol, binary_mask)
            p_difficulty = p_difficulty * valid_mask
            range_error = (p_range - in_vol[:, 0:self.n_echoes, ...]) * valid_mask

            knn_values, _ = torch.sort(knn_values, dim=2)
            closest_n = knn_values[:, :, 1:3, ...].detach()
            mean_dist = torch.mean(closest_n, dim=2)
            sparsity = mean_dist / proj_range

            regression_target = torch.zeros((B, 0, H, W)).cuda()
            regression_var = torch.zeros((B, 0, H, W)).cuda()
            for echo in range(self.n_echoes):
                n_si = torch.cat((sparsity[:, [echo], ...], intensity[:, [echo], ...]), dim=1)
                n_si = n_si * valid_mask[:, [echo], ...]
                n_p_difficulty, n_si_dist = self.knn_search.KNNs(n_si, p_difficulty[:, [echo], ...])
                n_regression_target = n_p_difficulty.mean(dim=1) * valid_mask[:, [echo], ...]
                n_regression_var = n_p_difficulty.std(dim=1) * valid_mask[:, [echo], ...]
                n_regression_var = torch.clamp(n_regression_var, min=1, max=2)
                
                regression_target = torch.cat((regression_target, n_regression_target), dim=1)
                regression_var = torch.cat((regression_var, n_regression_var), dim=1)

            proj_range = torch.bucketize(proj_range, torch.arange(1,80,1).cuda()) + 1

            loss_m = ((torch.abs(range_error)/(0.2*proj_range*torch.exp(p_difficulty)) + torch.abs(regression_target - p_difficulty)/regression_var + p_difficulty)*binary_mask*valid_mask).sum()/torch.count_nonzero(binary_mask*valid_mask)
            
            optimizer.zero_grad()
            optimizer2.zero_grad()
            if self.n_gpus > 1:
                idx = torch.ones(self.n_gpus).cuda()
                loss_m.backward(idx)
            else:
                loss_m.backward()
            optimizer.step()
            optimizer2.step()

            # measure accuracy and record loss
            loss = loss_m.mean()
            with torch.no_grad():
                evaluator.reset()
                accuracy = evaluator.getacc()
                jaccard, class_jaccard = evaluator.getIoU()

            losses.update(loss.item(), in_vol.size(0))
            acc.update(accuracy.item(), in_vol.size(0))
            iou.update(jaccard.item(), in_vol.size(0))

            # measure elapsed time
            self.batch_time_t.update(time.time() - end)
            end = time.time()

            # get gradient updates and weights, so I can print the relationship of
            # their norms
            update_ratios = []
            for g in self.optimizer.param_groups:
                lr = g["lr"]
                for value in g["params"]:
                    if value.grad is not None:
                        w = np.linalg.norm(value.data.cpu().numpy().reshape((-1)))
                        update = np.linalg.norm(-max(lr, 1e-10) *
                                                value.grad.cpu().numpy().reshape((-1)))
                        update_ratios.append(update / max(w, 1e-10))
            update_ratios = np.array(update_ratios)
            update_mean = update_ratios.mean()
            update_std = update_ratios.std()
            update_ratio_meter.update(update_mean)  # over the epoch

            if show_scans:
                # get the first scan in batch and project points
                mask_np = proj_mask[0].cpu().numpy()
                depth_np = in_vol[0][0].cpu().numpy()
                pred_np = argmax[0].cpu().numpy()
                gt_np = proj_labels[0].cpu().numpy()
                out = Trainer.make_log_img(depth_np, mask_np, pred_np, gt_np, color_fn)

                mask_np = proj_mask[1].cpu().numpy()
                depth_np = in_vol[1][0].cpu().numpy()
                pred_np = argmax[1].cpu().numpy()
                gt_np = proj_labels[1].cpu().numpy()
                out2 = Trainer.make_log_img(depth_np, mask_np, pred_np, gt_np, color_fn)

                out = np.concatenate([out, out2], axis=0)
                cv2.imshow("sample_training", out)
                cv2.waitKey(1)
            
            if i % self.ARCH["train"]["report_batch"] == 0:
                print('Lr: {lr:.3e} | '
                      'Update: {umean:.3e} mean,{ustd:.3e} std | '
                      'Epoch: [{0}][{1}/{2}] | '
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f}) | '
                      'Data {data_time.val:.3f} ({data_time.avg:.3f}) | '
                      'Loss {loss.val:.4f} ({loss.avg:.4f}) | '
                      'acc {acc.val:.3f} ({acc.avg:.3f}) | '
                      'IoU {iou.val:.3f} ({iou.avg:.3f}) | [{estim}]'.format(
                    epoch, i, len(train_loader), batch_time=self.batch_time_t,
                    data_time=self.data_time_t, loss=losses, acc=acc, iou=iou, lr=lr,
                    umean=update_mean, ustd=update_std, estim=self.calculate_estimate(epoch, i)))

                save_to_log(self.log, 'log.txt', 'Lr: {lr:.3e} | '
                                                 'Update: {umean:.3e} mean,{ustd:.3e} std | '
                                                 'Epoch: [{0}][{1}/{2}] | '
                                                 'Time {batch_time.val:.3f} ({batch_time.avg:.3f}) | '
                                                 'Data {data_time.val:.3f} ({data_time.avg:.3f}) | '
                                                 'Loss {loss.val:.4f} ({loss.avg:.4f}) | '
                                                 'acc {acc.val:.3f} ({acc.avg:.3f}) | '
                                                 'IoU {iou.val:.3f} ({iou.avg:.3f}) | [{estim}]'.format(
                    epoch, i, len(train_loader), batch_time=self.batch_time_t,
                    data_time=self.data_time_t, loss=losses, acc=acc, iou=iou, lr=lr,
                    umean=update_mean, ustd=update_std, estim=self.calculate_estimate(epoch, i)))

            # step scheduler
            scheduler.step()
            scheduler2.step()

        return acc.avg, iou.avg, losses.avg, update_ratio_meter.avg,hetero_l.avg

    def validate(self, val_loader, model, teacher, criterion, evaluator, class_func, color_fn, save_scans):
        losses = AverageMeter()
        jaccs = AverageMeter()
        wces = AverageMeter()
        acc = AverageMeter()
        iou = AverageMeter()
        hetero_l = AverageMeter()
        rand_imgs = []

        # switch to evaluate mode
        model.eval()
        evaluator.reset()

        # empty the cache to infer in high res
        if self.gpu:
            torch.cuda.empty_cache()

        with torch.no_grad():
            end = time.time()
            for i, (in_vol, pre_in_vol, proj_mask, proj_labels, _, path_seq, path_name, _, _, _, _, _, _, _, _, _, _) in enumerate(val_loader):
                if not self.multi_gpu and self.gpu:
                    in_vol = in_vol.cuda()
                    proj_mask = proj_mask.cuda()
                    pre_in_vol = pre_in_vol.cuda()
                if self.gpu:
                    proj_labels = proj_labels.cuda(non_blocking=True).long()

                # compute output
                output = model(in_vol)

                # measure accuracy and record loss
                valid_mask = (in_vol[:, [0], ...] != 0).int()
                argmax = ((output > -0.15)*valid_mask).long() + 1
                evaluator.addBatch(argmax, proj_labels)
                
                if save_scans:
                    # get the first scan in batch and project points
                    mask_np = proj_mask[0].cpu().numpy()
                    depth_np = in_vol[0][0].cpu().numpy()
                    pred_np = argmax[0].cpu().numpy()
                    gt_np = proj_labels[0].cpu().numpy()
                    out = Trainer.make_log_img(depth_np,
                                               mask_np,
                                               pred_np,
                                               gt_np,
                                               color_fn)
                    rand_imgs.append(out)

                # measure elapsed time
                self.batch_time_e.update(time.time() - end)
                end = time.time()

            accuracy = evaluator.getacc()
            jaccard, class_jaccard = evaluator.getIoU()
            acc.update(accuracy.item(), in_vol.size(0))
            iou.update(jaccard.item(), in_vol.size(0))
            
            print('Validation set:\n'
                  'Time avg per batch {batch_time.avg:.3f}\n'
                  'Loss avg {loss.avg:.4f}\n'
                  'Jaccard avg {jac.avg:.4f}\n'
                  'WCE avg {wces.avg:.4f}\n'
                  'Acc avg {acc.avg:.3f}\n'
                  'IoU avg {iou.avg:.3f}'.format(batch_time=self.batch_time_e,
                                                 loss=losses,
                                                 jac=jaccs,
                                                 wces=wces,
                                                 acc=acc, iou=iou))

            save_to_log(self.log, 'log.txt', 'Validation set:\n'
                                             'Time avg per batch {batch_time.avg:.3f}\n'
                                             'Loss avg {loss.avg:.4f}\n'
                                             'Jaccard avg {jac.avg:.4f}\n'
                                             'WCE avg {wces.avg:.4f}\n'
                                             'Acc avg {acc.avg:.3f}\n'
                                             'IoU avg {iou.avg:.3f}'.format(batch_time=self.batch_time_e,
                                                                            loss=losses,
                                                                            jac=jaccs,
                                                                            wces=wces,
                                                                            acc=acc, iou=iou))
            # print also classwise
            for i, jacc in enumerate(class_jaccard):
                print('IoU class {i:} [{class_str:}] = {jacc:.3f}'.format(
                    i=i, class_str=class_func(i), jacc=jacc))
                save_to_log(self.log, 'log.txt', 'IoU class {i:} [{class_str:}] = {jacc:.3f}'.format(
                    i=i, class_str=class_func(i), jacc=jacc))
                self.info["valid_classes/" + class_func(i)] = jacc


        return acc.avg, iou.avg, losses.avg, rand_imgs, hetero_l.avg
