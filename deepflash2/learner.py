# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/03_learner.ipynb (unless otherwise specified).

__all__ = ['EnsembleBase', 'EnsembleLearner', 'EnsemblePredictor']

# Cell
import torch
import time
import zarr
import pandas as pd
import numpy as np
import cv2
import tifffile
from pathlib import Path
from typing import List, Union, Tuple

from skimage.color import label2rgb
from sklearn.model_selection import KFold

from fastprogress import progress_bar
from fastcore.basics import GetAttr
from fastcore.foundation import L
from fastai import optimizer
from fastai.learner import Learner
from fastai.callback.all import *
from fastai.callback.tracker import SaveModelCallback
from fastai.callback.progress import CSVLogger
from fastai.data.core import DataLoaders
from fastai.data.transforms import get_image_files, get_files

from .config import Config
from .data import BaseDataset, TileDataset, RandomTileDataset
from .models import create_smp_model, save_smp_model, load_smp_model, run_cellpose
from .inference import InferenceEnsemble
from .losses import get_loss
from .utils import compose_albumentations as _compose_albumentations
from .utils import dice_score, binary_dice_score, plot_results, get_label_fn, save_mask, save_unc, export_roi_set, get_instance_segmentation_metrics
from fastai.metrics import Dice, DiceMulti

import matplotlib.pyplot as plt
import warnings

#https://discuss.pytorch.org/t/slow-forward-on-traced-graph-on-cuda-2nd-iteration/118445/7
try: torch._C._jit_set_fusion_strategy([('STATIC', 0)])
except: torch._C._jit_set_bailout_depth(0)

# Cell
_optim_dict = {
    'ranger' : optimizer.ranger,
    'Adam' : optimizer.Adam,
    'RAdam' : optimizer.RAdam,
    'QHAdam' :optimizer.QHAdam,
    'Larc' : optimizer.Larc,
    'Lamb' : optimizer.Lamb,
    'SGD' : optimizer.SGD,
    'RMSProp' : optimizer.RMSProp,
}

# Cell
class EnsembleBase(GetAttr):
    _default = 'config'
    def __init__(self, image_dir:str=None, mask_dir:str=None, files:List[Path]=None, label_fn:callable=None,
                 config:Config=None, path:Path=None, zarr_store:str=None):

        self.config = config or Config()
        self.path = Path(path) if path is not None else Path('.')
        self.label_fn = None
        self.files = L()

        store = str(zarr_store) if zarr_store else zarr.storage.TempStore()
        root = zarr.group(store=store, overwrite=False)
        self.store = root.chunk_store.path
        self.g_pred, self.g_smx, self.g_std  = root.require_groups('preds', 'smxs', 'stds')

        if any(v is not None for v in (image_dir, files)):
            self.files = L(files) or self.get_images(image_dir)

            if any(v is not None for v in (mask_dir, label_fn)):
                assert hasattr(self, 'files'), 'image_dir or files must be provided'
                self.label_fn = label_fn or self.get_label_fn(mask_dir)
                self.check_label_fn()

    def get_images(self, img_dir:str='images', img_path:Path=None) -> List[Path]:
        'Returns list of image paths'
        path = img_path or self.path/img_dir
        files = get_image_files(path, recurse=False)
        print(f'Found {len(files)} images in "{path}".')
        if len(files)==0: warnings.warn('Please check your provided images and image folder')
        return files

    def get_label_fn(self, msk_dir:str='masks', msk_path:Path=None):
        'Returns label function to get paths of masks'
        path = msk_path or self.path/msk_dir
        return get_label_fn(self.files[0], path)

    def check_label_fn(self):
        'Checks label function'
        mask_check = [self.label_fn(x).exists() for x in self.files]
        chk_str = f'Found {sum(mask_check)} corresponding masks.'
        print(chk_str)
        if len(self.files)!=sum(mask_check):
            warnings.warn(f'Please check your images and masks (and folders).')

    def predict(self, arr:Union[np.ndarray, torch.Tensor]) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        'Get prediction for arr using inference_ensemble'
        inp = torch.tensor(arr).float().to(self.device)
        with torch.inference_mode():
            preds = self.inference_ensemble(inp)
        preds = [x.cpu().numpy() for x in preds]
        return tuple(preds)

    def save_preds_zarr(self, f_name, pred, smx, std):
        self.g_pred[f_name] = pred
        self.g_smx[f_name] = smx
        self.g_std[f_name] = std

    def _create_ds(self, **kwargs):
        self.ds = BaseDataset(self.files, label_fn=self.label_fn, instance_labels=self.instance_labels,
                              num_classes=self.num_classes, **kwargs)

# Cell
class EnsembleLearner(EnsembleBase):
    "Meta class to training model ensembles with `n` models"
    def __init__(self, *args, ensemble_path=None, preproc_dir=None, metrics=None, cbs=None,
                 ds_kwargs={}, dl_kwargs={}, model_kwargs={}, stats=None, **kwargs):
        super().__init__(*args, **kwargs)

        assert hasattr(self, 'label_fn'), 'mask_dir or label_fn must be provided.'
        self.stats = stats
        self.dl_kwargs = dl_kwargs
        self.model_kwargs = model_kwargs
        self.add_ds_kwargs = ds_kwargs
        default_metrics = [Dice()] if self.num_classes==2 else [DiceMulti()]
        self.metrics = metrics or default_metrics
        self.loss_fn = self.get_loss()
        self.cbs = cbs or [SaveModelCallback(monitor='dice' if self.num_classes==2 else 'dice_multi')] #ShowGraphCallback
        self.ensemble_dir = ensemble_path or self.path/self.ens_dir
        if ensemble_path is not None:
            ensemble_path.mkdir(exist_ok=True, parents=True)
            self.load_models(path=ensemble_path)
        else: self.models = {}

        self.n_splits=min(len(self.files), self.max_splits)
        self._set_splits()
        self._create_ds(stats=self.stats, preproc_dir=preproc_dir, verbose=1, **self.add_ds_kwargs)
        self.stats = self.ds.stats
        self.in_channels = self.ds.get_data(max_n=1)[0].shape[-1]
        self.df_val, self.df_ens, self.df_model, self.ood = None,None,None,None
        self.recorder = {}

    def _set_splits(self):
        if self.n_splits>1:
            kf = KFold(self.n_splits, shuffle=True, random_state=self.random_state)
            self.splits = {key:(self.files[idx[0]], self.files[idx[1]]) for key, idx in zip(range(1,self.n_splits+1), kf.split(self.files))}
        else:
            self.splits = {1: (self.files[0], self.files[0])}

    def _compose_albumentations(self, **kwargs):
        return _compose_albumentations(**kwargs)

    @property
    def pred_ds_kwargs(self):
        # Setting default shapes and padding
        ds_kwargs = self.add_ds_kwargs.copy()
        ds_kwargs['use_preprocessed_labels']= True
        ds_kwargs['preproc_dir']=self.ds.preproc_dir
        ds_kwargs['instance_labels']= self.instance_labels
        ds_kwargs['tile_shape']= (self.tile_shape,)*2
        ds_kwargs['num_classes']= self.num_classes
        ds_kwargs['max_tile_shift']= self.max_tile_shift
        ds_kwargs['scale']= self.scale
        ds_kwargs['border_padding_factor']= self.border_padding_factor
        return ds_kwargs

    @property
    def train_ds_kwargs(self):
        # Setting default shapes and padding
        ds_kwargs = self.add_ds_kwargs.copy()
        # Settings from config
        ds_kwargs['use_preprocessed_labels']= True
        ds_kwargs['preproc_dir']=self.ds.preproc_dir
        ds_kwargs['instance_labels']= self.instance_labels
        ds_kwargs['stats']= self.stats
        ds_kwargs['tile_shape']= (self.tile_shape,)*2
        ds_kwargs['num_classes']= self.num_classes
        ds_kwargs['scale']= self.scale
        ds_kwargs['flip'] = self.flip
        ds_kwargs['max_tile_shift']= 1.
        ds_kwargs['border_padding_factor']= 0.
        ds_kwargs['scale']= self.scale
        ds_kwargs['albumentations_tfms'] = self._compose_albumentations(**self.albumentation_kwargs)
        ds_kwargs['sample_mult'] = self.sample_mult if self.sample_mult>0 else None
        return ds_kwargs

    @property
    def model_name(self):
        encoder_name = self.encoder_name.replace('_', '-')
        return f'{self.arch}_{encoder_name}_{self.num_classes}classes'

    def get_loss(self):
        kwargs = {'mode':self.mode,
                  'classes':[x for x in range(1, self.num_classes)],
                  'smooth_factor': self.loss_smooth_factor,
                  'alpha':self.loss_alpha,
                  'beta':self.loss_beta,
                  'gamma':self.loss_gamma}
        return get_loss(self.loss, **kwargs)


    def _get_dls(self, files, files_val=None):
        ds = []
        ds.append(RandomTileDataset(files, label_fn=self.label_fn, **self.train_ds_kwargs, verbose=0))
        if files_val:
            ds.append(TileDataset(files_val, label_fn=self.label_fn, **self.train_ds_kwargs, verbose=0))
        else:
            ds.append(ds[0])
        dls = DataLoaders.from_dsets(*ds, bs=self.batch_size, pin_memory=True, **self.dl_kwargs).to(self.device)
        return dls

    def _create_model(self):
        model = create_smp_model(arch=self.arch,
                                 encoder_name=self.encoder_name,
                                 encoder_weights=self.encoder_weights,
                                 in_channels=self.in_channels,
                                 classes=self.num_classes,
                                 **self.model_kwargs).to(self.device)
        return model


    def fit(self, i, n_epochs=None, base_lr=None, **kwargs):
        'Fit model number `i`'
        n_epochs = n_epochs or self.n_epochs
        base_lr = base_lr or self.base_lr
        name = self.ensemble_dir/'single_models'/f'{self.model_name}-fold{i}.pth'
        model = self._create_model()
        files_train, files_val = self.splits[i]
        dls = self._get_dls(files_train, files_val)
        log_name = f'{name.name}_{time.strftime("%Y%m%d-%H%M%S")}.csv'
        log_dir = self.ensemble_dir/'logs'
        log_dir.mkdir(exist_ok=True, parents=True)
        cbs = self.cbs + [CSVLogger(fname=log_dir/log_name)]
        self.learn = Learner(dls, model,
                             metrics=self.metrics,
                             wd=self.weight_decay,
                             loss_func=self.loss_fn,
                             opt_func=_optim_dict[self.optim],
                             cbs=cbs)
        self.learn.model_dir = self.ensemble_dir.parent/'.tmp'
        if self.mixed_precision_training: self.learn.to_fp16()
        print(f'Starting training for {name.name}')
        self.learn.fine_tune(n_epochs, base_lr=base_lr)

        print(f'Saving model at {name}')
        name.parent.mkdir(exist_ok=True, parents=True)
        save_smp_model(self.learn.model, self.arch, name, stats=self.stats)
        self.models[i]=name
        self.recorder[i]=self.learn.recorder

        del model
        if torch.cuda.is_available(): torch.cuda.empty_cache()

    def get_inference_ensemble(self, model_path=None):
        model_paths = [model_path] if model_path is not None else self.models.values()
        models = [load_smp_model(p)[0] for p in model_paths]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ensemble = InferenceEnsemble(models,
                                         num_classes=self.num_classes,
                                         in_channels=self.in_channels,
                                         channel_means=self.stats['channel_means'].tolist(),
                                         channel_stds=self.stats['channel_stds'].tolist(),
                                         tile_shape=(self.tile_shape,)*2,
                                         **self.inference_kwargs).to(self.device)
        return torch.jit.script(ensemble)

    def save_inference_ensemble(self):
        ensemble = self.get_inference_ensemble()
        ensemble_name = self.ensemble_dir/f'ensemble_{self.model_name}.pt'
        print(f'Saving model at {ensemble_name}')
        ensemble.save(ensemble_name)

    def fit_ensemble(self, n_epochs=None, skip=False, save_inference_ensemble=True, **kwargs):
        'Fit `i` models and `skip` existing'
        for i in range(1, self.n_models+1):
            if skip and (i in self.models): continue
            self.fit(i, n_epochs,  **kwargs)
        if save_inference_ensemble: self.save_inference_ensemble()

    def set_n(self, n):
        "Change to `n` models per ensemble"
        for i in range(n, len(self.models)):
            self.models.pop(i+1, None)
        self.n_models = n

    def get_valid_results(self, model_no=None, zarr_store=None, export_dir=None, filetype='.png', **kwargs):
        "Validate models on validation data and save results"
        res_list = []
        model_dict = self.models if not model_no else {k:v for k,v in self.models.items() if k==model_no}
        metric_name = 'dice_score' if self.num_classes==2 else 'average_dice_score'

        if export_dir:
            export_dir = Path(export_dir)
            pred_path = export_dir/'masks'
            pred_path.mkdir(parents=True, exist_ok=True)
            unc_path = export_dir/'uncertainties'
            unc_path.mkdir(parents=True, exist_ok=True)

        for i, model_path in model_dict.items():
            print(f'Validating model {i}.')
            self.inference_ensemble = self.get_inference_ensemble(model_path=model_path)
            _, files_val = self.splits[i]

            for j, f in progress_bar(enumerate(files_val), total=len(files_val)):

                pred, smx, std = self.predict(self.ds.data[f.name][:])
                self.save_preds_zarr(f.name, pred, smx, std)
                msk = self.ds.labels[f.name][:] #.get_data(f, mask=True)[0])
                m_dice = dice_score(msk, pred, num_classes=self.num_classes)
                df_tmp = pd.Series({'file' : f.name,
                        'model' :  model_path,
                        'model_no' : i,
                        metric_name: m_dice,
                        'uncertainty_score': np.mean(std[pred>0]),
                        'image_path': f,
                        'mask_path': self.label_fn(f),
                        'pred_path': f'{self.store}/{self.g_pred.path}/{f.name}',
                        'softmax_path': f'{self.store}/{self.g_smx.path}/{f.name}',
                        'uncertainty_path': f'{self.store}/{self.g_std.path}/{f.name}'})
                res_list.append(df_tmp)
                if export_dir:
                    save_mask(pred, pred_path/f'{df_tmp.file}_model{df_tmp.model_no}_mask', filetype)
                    save_unc(std, unc_path/f'{df_tmp.file}_model{df_tmp.model_no}_uncertainty', filetype)

        del self.inference_ensemble
        if torch.cuda.is_available(): torch.cuda.empty_cache()

        self.df_val = pd.DataFrame(res_list)
        if export_dir:
            self.df_val.to_csv(export_dir/f'val_results.csv', index=False)
            self.df_val.to_excel(export_dir/f'val_results.xlsx')
        return self.df_val

    def show_valid_results(self, model_no=None, files=None, metric_name='auto', **kwargs):
        "Plot results of all or `file` validation images",
        if self.df_val is None: self.get_valid_results(**kwargs)
        df = self.df_val
        if files is not None: df = df.set_index('file', drop=False).loc[files]
        if model_no is not None: df = df[df.model_no==model_no]
        if metric_name=='auto': metric_name = 'dice_score' if self.num_classes==2 else 'average_dice_score'
        for _, r in df.iterrows():
            img = self.ds.data[r.file][:]
            msk = self.ds.labels[r.file][:]
            pred = self.g_pred[r.file][:]
            std = self.g_std[r.file][:]
            _d_model = f'Model {r.model_no}'
            plot_results(img, msk, pred, std, df=r, num_classes=self.num_classes, metric_name=metric_name, model=_d_model)

    def load_models(self, path=None):
        "Get models saved at `path`"
        path = path or self.ensemble_dir/'single_models'
        models = sorted(get_files(path, extensions='.pth', recurse=False))
        self.models = {}

        for i, m in enumerate(models,1):
            if i==0: self.num_classes = int(m.name.split('_')[2][0])
            else: assert self.num_classes==int(m.name.split('_')[2][0]), 'Check models. Models are trained on different number of classes.'
            self.models[i] = m

        if len(self.models)>0:
            self.set_n(len(self.models))
            print(f'Found {len(self.models)} models in folder {path}:')
            print([m.name for m in self.models.values()])

            # Reset stats
            print(f'Loading stats from {self.models[1].name}')
            _, self.stats = load_smp_model(self.models[1])

    def lr_find(self, files=None, **kwargs):
        "Wrapper function for learning rate finder"
        files = files or self.files
        dls = self._get_dls(files)
        model = self._create_model()
        learn = Learner(dls, model, metrics=self.metrics, wd=self.weight_decay, loss_func=self.loss_fn, opt_func=_optim_dict[self.optim])
        if self.mixed_precision_training: learn.to_fp16()
        sug_lrs = learn.lr_find(**kwargs)
        return sug_lrs, learn.recorder

# Cell
class EnsemblePredictor(EnsembleBase):
    def __init__(self, *args, ensemble_path:Path=None, **kwargs):
        if ensemble_path is not None:
            self.load_inference_ensemble(ensemble_path)

        super().__init__(*args, **kwargs)

        if hasattr(self, 'inference_ensemble'):
            self.config.num_classes = self.inference_ensemble.num_classes

        if hasattr(self, 'files'):
            self._create_ds(stats={}, use_zarr_data = False, verbose=1)

        self.ensemble_dir = self.path/self.ens_dir

        #if ensemble_path is not None:
        #    self.load_inference_ensemble(ensemble_path)

    def load_inference_ensemble(self, ensemble_path:Path=None):
        "Load inference_ensemble from `self.ensemle_dir` or from `path`"
        path = ensemble_path or self.ensemble_dir
        if path.is_dir():
            path_list = get_files(path, extensions='.pt', recurse=False)
            if len(path_list)==0:
                warnings.warn(f'No inference ensemble available at {path}. Did you train your ensemble correctly?')
                return
            path = path_list[0]
        self.inference_ensemble_name = path.name
        if hasattr(self, 'device'): self.inference_ensemble = torch.jit.load(path).to(self.device)
        else: self.inference_ensemble = torch.jit.load(path)
        print(f'Successfully loaded InferenceEnsemble from {path}')


    def get_ensemble_results(self, file_list=None, export_dir=None, filetype='.png', **kwargs):
        'Predict files in file_list using InferenceEnsemble'

        if file_list is not None:
            self.files = file_list
            self._create_ds(stats={}, use_zarr_data = False, verbose=1)

        if export_dir:
            export_dir = Path(export_dir)
            pred_path = export_dir/'masks'
            pred_path.mkdir(parents=True, exist_ok=True)
            unc_path = export_dir/'uncertainties'
            unc_path.mkdir(parents=True, exist_ok=True)

        res_list = []
        for f in progress_bar(self.files):
            img = self.ds.read_img(f)
            pred, smx, std = self.predict(img)
            self.save_preds_zarr(f.name, pred, smx, std)
            df_tmp = pd.Series({'file' : f.name,
                                'ensemble' : self.inference_ensemble_name,
                                'uncertainty_score': np.mean(std[pred>0]),
                                'image_path': f,
                                'pred_path': f'{self.store}/{self.g_pred.path}/{f.name}',
                                'softmax_path': f'{self.store}/{self.g_smx.path}/{f.name}',
                                'uncertainty_path': f'{self.store}/{self.g_std.path}/{f.name}'})
            res_list.append(df_tmp)
            if export_dir:
                save_mask(pred, pred_path/f'{df_tmp.file}_mask', filetype)
                save_unc(std, unc_path/f'{df_tmp.file}_unc', filetype)

        self.df_ens  = pd.DataFrame(res_list)
        return self.g_pred, self.g_smx, self.g_std

    def score_ensemble_results(self, mask_dir=None, label_fn=None):
        "Compare ensemble results to given segmentation masks."

        if any(v is not None for v in (mask_dir, label_fn)):
            self.label_fn = label_fn or self.get_label_fn(mask_dir)
            self._create_ds(stats={}, use_zarr_data = False, verbose=1)

        print('Calculating metrics')
        for i, r in progress_bar(self.df_ens.iterrows(), total=len(self.df_ens)):
            msk = self.ds.labels[r.file][:]
            pred = self.g_pred[r.file][:]

            if self.num_classes==2:
                self.df_ens.loc[i, f'dice_score'] = binary_dice_score(msk, pred)
            else:
                for cl in range(self.num_classes):
                    msk_bin = msk==cl
                    pred_bin = pred==cl
                    if np.any([msk_bin, pred_bin]):
                        self.df_ens.loc[i, f'dice_score_class{cl}'] = binary_dice_score(msk_bin, pred_bin)

        if self.num_classes>2:
            self.df_ens['average_dice_score'] = self.df_ens[[col for col in self.df_ens if col.startswith('dice_score_class')]].mean(axis=1)

        return self.df_ens

    def show_ensemble_results(self, files=None, unc=True, unc_metric=None, metric_name='auto'):
        "Show result of ensemble or `model_no`"
        assert self.df_ens is not None, "Please run `get_ensemble_results` first."
        df = self.df_ens
        if files is not None: df = df.reset_index().set_index('file', drop=False).loc[files]
        if metric_name=='auto': metric_name = 'dice_score' if self.num_classes==2 else 'average_dice_score'
        for _, r in df.iterrows():
            imgs = []
            imgs.append(self.ds.read_img(r.image_path))
            if metric_name in r.index:
                imgs.append(self.ds.labels[r.file][:])
                hastarget=True
            else:
                hastarget=False
            imgs.append(self.g_pred[r.file])
            if unc: imgs.append(self.g_std[r.file])
            plot_results(*imgs, df=r, hastarget=hastarget, num_classes=self.num_classes, metric_name=metric_name, unc_metric=unc_metric)


    def get_cellpose_results(self, export_dir=None, check_missing=True):
        'Get instance segmentation results using the cellpose integration'
        assert self.df_ens is not None, "Please run `get_ensemble_results` first."
        cl = self.cellpose_export_class
        assert cl<self.num_classes, f'{cl} not avaialable from {self.num_classes} classes'

        smxs, preds = [], []
        for _, r in self.df_ens.iterrows():
            smxs.append(self.g_smx[r.file][:])
            preds.append(self.g_pred[r.file][:])

        probs = [x[cl] for x in smxs]
        masks = [x==cl for x in preds]
        cp_masks = run_cellpose(probs, masks,
                                model_type=self.cellpose_model,
                                diameter=self.cellpose_diameter,
                                min_size=self.min_pixel_export,
                                flow_threshold=self.cellpose_flow_threshold,
                                gpu=torch.cuda.is_available())

        # Check for missing pixels in cellpose masks
        if check_missing:
            for i, _ in self.df_ens.iterrows():
                cp_mask_bin = (cp_masks[i]>0).astype('uint8')
                n_diff = np.sum(masks[i]!=cp_mask_bin, dtype='uint8')
                self.df_ens.at[i,f'cellpose_removed_pixels_class{cl}'] = n_diff

        if export_dir:
            export_dir = Path(export_dir)/'instance_labels'
            export_dir.mkdir(parents=True, exist_ok=True)
            for idx, r in self.df_ens.iterrows():
                tifffile.imwrite(export_dir/f'{r.file}_class{cl}.tif', cp_masks[idx], compress=6)

        self.cellpose_masks = cp_masks
        return cp_masks

    def score_cellpose_results(self, mask_dir=None, label_fn=None):
        "Compare cellpose nstance segmentation results to given masks."
        assert self.cellpose_masks is not None, 'Run get_cellpose_results() first'
        if any(v is not None for v in (mask_dir, label_fn)):
            self.label_fn = label_fn or self.get_label_fn(mask_dir)
            self._create_ds(stats={}, use_zarr_data = False, verbose=1)

        cl = self.cellpose_export_class
        for i, r in self.df_ens.iterrows():
            msk = self.ds.labels[r.file][:]==cl
            _, msk = cv2.connectedComponents(msk.astype('uint8'), connectivity=4)
            pred = self.cellpose_masks[i]
            ap, tp, fp, fn = get_instance_segmentation_metrics(msk, pred, is_binary=False, min_pixel=self.min_pixel_export)
            self.df_ens.loc[i, f'mAP_class{cl}'] = ap.mean()
            self.df_ens.loc[i, f'mAP_iou50_class{cl}'] = ap[0]
        return self.df_ens


    def show_cellpose_results(self, files=None, unc_metric=None, metric_name='auto'):
        'Show instance segmentation results from cellpose predictions.'
        assert self.df_ens is not None, "Please run `get_ensemble_results` first."
        df = self.df_ens.reset_index()
        if files is not None: df = df.set_index('file', drop=False).loc[files]
        if metric_name=='auto': metric_name=f'mAP_class{self.cellpose_export_class}'
        for _, r in df.iterrows():
            imgs = [self.ds.read_img(r.image_path)]
            if metric_name in r.index:
                mask = self.ds.labels[r.file][:]
                mask = (mask==self.cellpose_export_class).astype('uint8')
                _, comps = cv2.connectedComponents(mask, connectivity=4)
                imgs.append(label2rgb(comps, bg_label=0))
                hastarget=True
            else:
                hastarget=False

            imgs.append(label2rgb(self.cellpose_masks[r['index']], bg_label=0))
            imgs.append(self.g_std[r.file])
            plot_results(*imgs, df=r, hastarget=hastarget, num_classes=self.num_classes, instance_labels=True, metric_name=metric_name, unc_metric=unc_metric)

    def export_imagej_rois(self, output_folder='ROI_sets', **kwargs):
        'Export ImageJ ROI Sets to `ouput_folder`'
        assert self.df_ens is not None, "Please run prediction first."

        output_folder = Path(output_folder)
        output_folder.mkdir(exist_ok=True, parents=True)
        for idx, r in progress_bar(self.df_ens.iterrows(), total=len(self.df_ens)):
            pred = self.g_pred[r.file][:]
            uncertainty = self.g_std[r.file][:]
            export_roi_set(pred, uncertainty, name=r.file, path=output_folder, ascending=False, **kwargs)

    def export_cellpose_rois(self, output_folder='cellpose_ROI_sets', **kwargs):
        'Export cellpose predictions to ImageJ ROI Sets in `ouput_folder`'
        output_folder = Path(output_folder)
        output_folder.mkdir(exist_ok=True, parents=True)
        for idx, r in progress_bar(self.df_ens.iterrows(), total=len(self.df_ens)):
            pred = self.cellpose_masks[idx]
            uncertainty = self.g_std[r.file][:]
            export_roi_set(pred, uncertainty, instance_labels=True, name=r.file, path=output_folder, ascending=False, **kwargs)
