import torch
import tqdm
from core.base_model import BaseModel
from core.logger import LogTracker
import copy
from thop import profile
from thop import clever_format
import numpy as np

class EMA():
    def __init__(self, beta=0.9999):
        super().__init__()
        self.beta = beta
    def update_model_average(self, ma_model, current_model):
        for current_params, ma_params in zip(current_model.parameters(), ma_model.parameters()):
            old_weight, up_weight = ma_params.data, current_params.data
            ma_params.data = self.update_average(old_weight, up_weight)
    def update_average(self, old, new):
        if old is None:
            return new
        return old * self.beta + (1 - self.beta) * new

class RIHD(BaseModel):
    def __init__(self, networks, optimizers, lr_schedulers, losses, sample_num, task, ema_scheduler=None, **kwargs):
        ''' must to init BaseModel with kwargs '''
        super(RIHD, self).__init__(**kwargs)

        ''' networks, dataloder, optimizers, losses, etc. '''
        self.loss_fn = losses[0]
        self.netG = networks[0] #整个就是Network,分为sr3和diffusion model两类
        if ema_scheduler is not None:
            self.ema_scheduler = ema_scheduler
            self.netG_EMA = copy.deepcopy(self.netG)
            self.EMA = EMA(beta=self.ema_scheduler['ema_decay'])
        else:
            self.ema_scheduler = None
        ''' ddp '''
        self.netG = self.set_device(self.netG, distributed=self.opt['distributed'])
        if self.ema_scheduler is not None:
            self.netG_EMA = self.set_device(self.netG_EMA, distributed=self.opt['distributed'])
        
        self.schedulers = lr_schedulers
        self.optG = optimizers[0]

        ''' networks can be a list, and must convert by self.set_device function if using multiple GPU. '''
        self.load_everything()
        if self.opt['distributed']:
            self.netG.module.set_loss(self.loss_fn)
            self.netG.module.set_new_noise_schedule(phase=self.phase)
        else:
            self.netG.set_loss(self.loss_fn)
            self.netG.set_new_noise_schedule(phase=self.phase)

        ''' can rewrite in inherited class for more informations logging '''
        self.train_metrics = LogTracker(*[m.__name__ for m in losses], phase='train')
        self.val_metrics = LogTracker(*[m.__name__ for m in self.metrics], phase='val')
        self.test_metrics = LogTracker(*[m.__name__ for m in self.metrics], phase='test')

        self.sample_num = sample_num
        self.task = task
        #self.evaluate_efficiency(image_size = 256)
        #self.evaluate_inference_speed(image_size=256)


    def evaluate_efficiency(self,image_size = 256):
        size = image_size
        gt = torch.randn((1,3,size,size)).cuda()
        cond = torch.randn(1,3,size,size).cuda()
        mask = torch.randn(1,1,size,size).cuda()

        flops, params = profile(self.netG, inputs=(gt, cond, mask))
        flops, params = clever_format([flops, params], '%.3f')

        print('params=', params)
        print('FLOPs=',flops)

    def evaluate_inference_speed(self, image_size=256):
        size = image_size
        gt = torch.randn((1,3,size,size)).cuda()
        cond = torch.randn(1,3,size,size).cuda()
        mask = torch.randn(1,1,size,size).cuda()

        starter, ender = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
        repetitions = 300
        timings=np.zeros((repetitions,1))
        #GPU-WARM-UP
        for _ in range(10):
            _ = self.netG(gt, cond, mask)             
        # MEASURE PERFORMANCE
        with torch.no_grad():
            for rep in range(repetitions):
                starter.record()
                _ = self.netG(gt, cond, mask)  
                ender.record()
                # WAIT FOR GPU SYNC
                torch.cuda.synchronize()
                curr_time = starter.elapsed_time(ender)
                timings[rep] = curr_time
        mean_syn = np.sum(timings) / repetitions
        std_syn = np.std(timings)
        mean_fps = 1000. / mean_syn
        print(' * Mean@1 {mean_syn:.3f}ms Std@5 {std_syn:.3f}ms FPS@1 {mean_fps:.2f}'.format(mean_syn=mean_syn, std_syn=std_syn, mean_fps=mean_fps))
        print(mean_syn)

    def set_input(self, data):
        ''' must use set_device in tensor '''
        self.cond_image = self.set_device(data.get('cond_image')) #条件图像，合成图像
        self.gt_image = self.set_device(data.get('gt_image')) #真实图像
        self.mask = self.set_device(data.get('mask')) #掩码图像
        self.path = data['path']
    
    def get_current_visuals(self, phase='train'):
        dict = {
            'gt_image': (self.gt_image.detach()[:].float().cpu()+1)/2,
            'cond_image': (self.cond_image.detach()[:].float().cpu()+1)/2,
            'mask': self.mask.detach()[:].float().cpu()
        }
        if phase != 'train':
            dict.update({
                'output': (self.output.detach()[:].float().cpu()+1)/2
            })
        return dict

    def save_current_results(self):
        ret_path = []
        ret_result = []
        for idx in range(self.batch_size):
            ret_path.append('In_{}'.format(self.path[idx]))
            ret_result.append(self.gt_image[idx].detach().float().cpu())

            ret_path.append('Process_{}'.format(self.path[idx]))
            ret_result.append(self.visuals[idx::self.batch_size].detach().float().cpu())
            
            ret_path.append('Out_{}'.format(self.path[idx]))
            ret_result.append(self.visuals[idx-self.batch_size].detach().float().cpu()) #这里记录的是idx减去batch_size的

        self.results_dict = self.results_dict._replace(name=ret_path, result=ret_result)
        return self.results_dict._asdict()

    def train_step(self):
        self.netG.train()
        self.train_metrics.reset()
        for train_data in tqdm.tqdm(self.phase_loader):
            self.set_input(train_data)
            self.optG.zero_grad()
            
            #for original ddpm
            #loss = self.netG(self.gt_image, self.cond_image, mask=self.mask) #输入分别是真实图片，条件图片（合成图片）和掩码
            #for improved ddpm
            losses = self.netG(self.gt_image, self.cond_image, mask=self.mask) #输入分别是真实图片，条件图片（合成图片）和掩码
            loss = losses.mean()

            loss.backward()
            self.optG.step()

            self.iter += self.batch_size
            self.writer.set_iter(self.epoch, self.iter, phase='train')
            self.train_metrics.update(self.loss_fn.__name__, loss.item())
            if self.iter % self.opt['train']['log_iter'] == 0:
                for key, value in self.train_metrics.result().items():
                    self.logger.info('{:5s}: {}\t'.format(str(key), value))
                    self.writer.add_scalar(key, value)
                for key, value in self.get_current_visuals().items():
                    self.writer.add_images(key, value)
            if self.ema_scheduler is not None:
                if self.iter % self.ema_scheduler['ema_iter'] == 0 and self.iter > self.ema_scheduler['ema_start']:
                    self.logger.info('Update the EMA  model at the iter {:.0f}'.format(self.iter))
                    self.EMA.update_model_average(self.netG_EMA, self.netG)

        for scheduler in self.schedulers:
            scheduler.step()
        return self.train_metrics.result()
    
    def val_step(self):
        self.netG.eval()
        self.val_metrics.reset()
        with torch.no_grad():
            for val_data in tqdm.tqdm(self.val_loader):
                self.set_input(val_data)
                if self.opt['distributed']:
                    if self.task in ['inpainting','uncropping']:
                        self.output, self.visuals = self.netG.module.restoration(self.cond_image, y_t=self.cond_image, 
                            y_0=self.gt_image, mask=self.mask, sample_num=self.sample_num)
                    else:
                        self.output, self.visuals = self.netG.module.restoration(self.cond_image, sample_num=self.sample_num)
                else:
                    if self.task in ['inpainting','uncropping']:
                        self.output, self.visuals = self.netG.restoration(self.cond_image, y_t=self.cond_image, 
                            y_0=self.gt_image, mask=self.mask, sample_num=self.sample_num)
                    else:
                        self.output, self.visuals = self.netG.restoration(self.cond_image, sample_num=self.sample_num)
                    
                self.iter += self.batch_size
                self.writer.set_iter(self.epoch, self.iter, phase='val')

                for met in self.metrics:
                    key = met.__name__
                    value = met(self.cond_image, self.output)
                    self.val_metrics.update(key, value)
                    self.writer.add_scalar(key, value)
                for key, value in self.get_current_visuals(phase='val').items():
                    self.writer.add_images(key, value)
                self.writer.save_images(self.save_current_results())

        return self.val_metrics.result()

    def test(self):
        self.netG.eval()
        self.test_metrics.reset()
        for phase_data in tqdm.tqdm(self.phase_loader):
            self.set_input(phase_data)
            if self.opt['distributed']:
                if self.task in ['inpainting','uncropping']:
                    self.output, self.visuals = self.netG.module.restoration(self.cond_image, y_t=self.cond_image, 
                        y_0=self.gt_image, mask=self.mask, sample_num=self.sample_num)
                else:
                    self.output, self.visuals = self.netG.module.restoration(self.cond_image, sample_num=self.sample_num)
            else:
                if self.task in ['inpainting','uncropping']:
                    self.output, self.visuals = self.netG.restoration(self.cond_image, y_t=self.cond_image, 
                        y_0=self.gt_image, mask=self.mask, sample_num=self.sample_num)
                else:
                    self.output, self.visuals = self.netG.restoration(self.cond_image, sample_num=self.sample_num)
                    
            self.iter += self.batch_size
            self.writer.set_iter(self.epoch, self.iter, phase='test')
            for met in self.metrics:
                key = met.__name__
                value = met(self.cond_image, self.output)
                self.val_metrics.update(key, value)
                self.writer.add_scalar(key, value)
            for key, value in self.get_current_visuals(phase='val').items():
                self.writer.add_images(key, value)
            self.writer.save_images(self.save_current_results())

    def load_everything(self):
        """ save pretrained model and training state, which only do on GPU 0. """
        if self.opt['distributed']:
            netG_label = self.netG.module.__class__.__name__
        else:
            netG_label = self.netG.__class__.__name__
        self.load_network(network=self.netG, network_label=netG_label, strict=False)
        if self.ema_scheduler is not None:
            self.load_network(network=self.netG_EMA, network_label=netG_label+'_ema', strict=False)
        self.resume_training([self.optG], self.schedulers) 

    def save_everything(self):
        """ load pretrained model and training state, optimizers and schedulers must be a list. """
        if self.opt['distributed']:
            netG_label = self.netG.module.__class__.__name__
        else:
            netG_label = self.netG.__class__.__name__
        self.save_network(network=self.netG, network_label=netG_label)
        if self.ema_scheduler is not None:
            self.save_network(network=self.netG_EMA, network_label=netG_label+'_ema')
        self.save_training_state([self.optG], self.schedulers)
