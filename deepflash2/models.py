# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/01_models.ipynb (unless otherwise specified).

__all__ = ['ARCHITECTURES', 'ENCODERS', 'get_pretrained_options', 'create_smp_model', 'save_smp_model',
           'load_smp_model', 'check_cellpose_installation', 'get_diameters', 'run_cellpose']

# Cell
import torch, numpy as np
import cv2
import segmentation_models_pytorch as smp
from fastcore.basics import patch
from fastdownload import download_url
from pathlib import Path
from pip._internal import main
from pip._internal.operations import freeze

# Cell
# https://github.com/qubvel/segmentation_models.pytorch#architectures-
ARCHITECTURES =  ['Unet', 'UnetPlusPlus', 'FPN', 'PAN', 'PSPNet', 'Linknet', 'DeepLabV3', 'DeepLabV3Plus'] #'MAnet',

# https://github.com/qubvel/segmentation_models.pytorch#encoders-
ENCODERS = [*smp.encoders.encoders.keys()]

# Cell
def get_pretrained_options(encoder_name):
    'Return available options for pretrained weights for a given encoder'
    options = smp.encoders.encoders[encoder_name]['pretrained_settings'].keys()
    return [*options, None]

# Cell
def create_smp_model(arch, **kwargs):
    'Create segmentation_models_pytorch model'

    assert arch in ARCHITECTURES, f'Select one of {ARCHITECTURES}'

    if arch=="Unet": model =  smp.Unet(**kwargs)
    elif arch=="UnetPlusPlus": model = smp.UnetPlusPlus(**kwargs)
    elif arch=="MAnet":model = smp.MAnet(**kwargs)
    elif arch=="FPN": model = smp.FPN(**kwargs)
    elif arch=="PAN": model = smp.PAN(**kwargs)
    elif arch=="PSPNet": model = smp.PSPNet(**kwargs)
    elif arch=="Linknet": model = smp.Linknet(**kwargs)
    elif arch=="DeepLabV3": model = smp.DeepLabV3(**kwargs)
    elif arch=="DeepLabV3Plus": model = smp.DeepLabV3Plus(**kwargs)
    else: raise NotImplementedError

    setattr(model, 'kwargs', kwargs)
    return model

# Cell
def save_smp_model(model, arch, path, stats=None, pickle_protocol=2):
    'Save smp model, optionally including  stats'
    path = Path(path)
    state = model.state_dict()
    save_dict = {'model': state, 'arch': arch, 'stats': stats, **model.kwargs}
    torch.save(save_dict, path, pickle_protocol=pickle_protocol, _use_new_zipfile_serialization=False)
    return path

# Cell
def load_smp_model(path, device=None, strict=True, **kwargs):
    'Loads smp model from file '
    path = Path(path)
    if isinstance(device, int): device = torch.device('cuda', device)
    elif device is None: device = 'cpu'
    model_dict = torch.load(path, map_location=device)
    state = model_dict.pop('model')
    stats = model_dict.pop('stats')
    model = create_smp_model(**model_dict)
    model.load_state_dict(state, strict=strict)
    return model, stats

# Cell
def check_cellpose_installation():
    tarball = 'cellpose-0.6.6.dev13+g316927e.tar.gz' # '316927eff7ad2201391957909a2114c68baee309'
    try:
        extract = [x for x in freeze.freeze() if x.startswith('cellpose')][0][-15:]
        assert extract==tarball[-15:]
    except:
        print(f'Installing cellpose. Please wait.')
        home_dir = Path.home()/'.deepflash2'
        home_dir.mkdir(exist_ok=True, parents=True)
        url = f'https://github.com/matjesg/deepflash2/releases/download/0.1.4/{tarball}'
        file = download_url(url, home_dir, show_progress=False)
        main(['install', '--no-deps', file.as_posix()])

# Cell
def get_diameters(masks):
    'Get diameters from deepflash2 prediction'
    from cellpose import utils
    diameters = []
    for m in masks:
        _, comps = cv2.connectedComponents(m.astype('uint8'), connectivity=4)
        diameters.append(utils.diameters(comps)[0])
    return int(np.array(diameters).mean())

# Cell
def run_cellpose(probs, masks, model_type='nuclei', diameter=0, min_size=-1, gpu=True):
    'Run cellpose on deepflash2 predictions'
    check_cellpose_installation()

    if diameter==0:
        diameter = get_diameters(masks)
    print(f'Using diameter of {diameter}')

    from cellpose import models, dynamics, utils
    @patch
    def _compute_masks(self:models.CellposeModel, dP, cellprob, p=None, niter=200,
                        flow_threshold=0.4, interp=True, do_3D=False, min_size=15, resize=None, **kwargs):
        """ compute masks using dynamics from dP and cellprob """
        if p is None:
            p = dynamics.follow_flows(-1 * dP * mask / 5., niter=niter, interp=interp, use_gpu=self.gpu)
        maski = dynamics.get_masks(p, iscell=mask, flows=dP, threshold=flow_threshold if not do_3D else None)
        maski = utils.fill_holes_and_remove_small_masks(maski, min_size=min_size)
        if resize is not None:
            maski = transforms.resize_image(maski, resize[0], resize[1],
                                            interpolation=cv2.INTER_NEAREST)
        return maski, p

    model = models.Cellpose(gpu=gpu, model_type=model_type)
    cp_masks = []
    for prob, mask in zip(probs, masks):
        cp_pred, _, _, _ = model.eval(prob,
                                       net_avg=True,
                                       augment=True,
                                       diameter=diameter,
                                       normalize=False,
                                       min_size=min_size,
                                       resample=True,
                                       channels=[0,0])
        cp_masks.append(cp_pred)
    return cp_masks