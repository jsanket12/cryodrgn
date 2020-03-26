'''
Evaluate a stack of particles on a model trained on vae_het.py
'''
import numpy as np
import sys, os
import argparse
import pickle
from datetime import datetime as dt
import json

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader

sys.path.insert(0,os.path.abspath(os.path.dirname(__file__))+'/lib-python')
import mrc
import utils
import fft
import lie_tools
import dataset
import ctf
import config

from pose import PoseTracker
from models import HetOnlyVAE
from lattice import Lattice
from beta_schedule import get_beta_schedule, LinearSchedule

from vae_het import preprocess_input, run_batch, loss_function

log = utils.log
vlog = utils.vlog

def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('particles', type=os.path.abspath, help='Input particles (.mrcs, .star, .cs, or .txt)')
    parser.add_argument('weights', help='Model weights')
    parser.add_argument('-c', '--config', metavar='PKL', help='CryoDRGN configuration')
    parser.add_argument('-o', metavar='PKL', help='Output pickle for z and losses')
    parser.add_argument('--poses', type=os.path.abspath, required=True, help='Image poses (.pkl)')
    parser.add_argument('--ctf', metavar='pkl', type=os.path.abspath, help='CTF parameters (.pkl) if particle stack is not phase flipped')
    parser.add_argument('--log-interval', type=int, default=1000, help='Logging interval in N_IMGS (default: %(default)s)')
    parser.add_argument('-b','--batch-size', type=int, default=50, help='Minibatch size (default: %(default)s)')
    parser.add_argument('--beta', default=1.0, type=float, help='KLD weight (default: %(default)s)')
    parser.add_argument('-v','--verbose',action='store_true',help='Increaes verbosity')

    group = parser.add_argument_group('Dataset loading')
    group.add_argument('--invert-data', action='store_true', help='Invert data sign')
    group.add_argument('--window', action='store_true', help='Real space windowing of dataset')
    group.add_argument('--ind', type=os.path.abspath, help='Filter particle stack by these indices')
    group.add_argument('--lazy', action='store_true', help='Lazy loading if full dataset is too large to fit in memory')
    group.add_argument('--datadir', type=os.path.abspath, help='Path prefix to particle stack if loading relative paths from a .star or .cs file')

    group = parser.add_argument_group('Tilt series')
    group.add_argument('--tilt', help='Particles (.mrcs)')
    group.add_argument('--tilt-deg', type=float, default=45, help='X-axis tilt offset in degrees (default: %(default)s)')

    group = parser.add_argument_group('Overwrite architecture hyperparameters in config.pkl')
    group.add_argument('--zdim', type=int,  help='Dimension of latent variable')
    group.add_argument('--norm', type=float, nargs=2, help='Data normalization as shift, 1/scale')
    group.add_argument('--qlayers', type=int,  help='Number of hidden layers')
    group.add_argument('--qdim', type=int, help='Number of nodes in hidden layers')
    group.add_argument('--encode-mode', choices=('conv','resid','mlp','tilt'), help='Type of encoder network')
    group.add_argument('--enc-mask', type=int, help='Circular mask of image for encoder')
    group.add_argument('--use-real', action='store_true', help='Use real space image for encoder (for convolutional encoder)')
    group.add_argument('--players', type=int, help='Number of hidden layers')
    group.add_argument('--pdim', type=int, help='Number of nodes in hidden layers')
    group.add_argument('--pe-type', choices=('geom_ft','geom_full','geom_lowf','geom_nohighf','linear_lowf','none'),  help='Type of positional encoding')
    group.add_argument('--domain', choices=('hartley','fourier'), help='Decoder representation domain')
    return parser
  
def eval_batch(model, lattice, y, yt, rot, trans, beta, tilt=None, ctf_params=None, yr=None):
    if trans is not None:
        y, yt = preprocess_input(y, yt, lattice, trans)
    z_mu, z_logvar, z, y_recon, y_recon_tilt, mask = run_batch(model, lattice, y, yt, rot, tilt, ctf_params, yr)
    loss, gen_loss, kld = loss_function(z_mu, z_logvar, y, yt, y_recon, mask, beta, y_recon_tilt, beta_control=None)
    return z_mu.detach().cpu().numpy(), z_logvar.detach().cpu().numpy(), loss.item(), gen_loss.item(), kld.item()

def main(args):
    t1 = dt.now()

    # set the device
    use_cuda = torch.cuda.is_available()
    device = torch.device('cuda' if use_cuda else 'cpu')
    log('Use cuda {}'.format(use_cuda))
    if use_cuda:
        torch.set_default_tensor_type(torch.cuda.FloatTensor)

    if args.config is not None:
        args = config.load_config(args.config, args)
    log(args)
    beta = args.beta

    # load the particles
    if args.ind is not None: 
        log('Filtering image dataset with {}'.format(args.ind))
        ind = pickle.load(open(args.ind,'rb'))
    else: ind = None

    if args.tilt is None:
        if args.encode_mode == 'conv':
            args.use_real = True
        if args.lazy:
            data = dataset.LazyMRCData(args.particles, norm=args.norm, invert_data=args.invert_data, ind=ind, keepreal=args.use_real, window=args.window, datadir=args.datadir)
        else:
            data = dataset.MRCData(args.particles, norm=args.norm, invert_data=args.invert_data, ind=ind, keepreal=args.use_real, window=args.window, datadir=args.datadir)
        tilt = None
    else:
        assert args.encode_mode == 'tilt'
        if args.lazy: raise NotImplementedError
        data = dataset.TiltMRCData(args.particles, args.tilt, norm=args.norm, invert_data=args.invert_data, ind=ind, window=args.window, keepreal=args.use_real, datadir=args.datadir)
        tilt = torch.tensor(utils.xrot(args.tilt_deg).astype(np.float32))
    Nimg = data.N
    D = data.D

    if args.encode_mode == 'conv':
        assert D-1 == 64, "Image size must be 64x64 for convolutional encoder"

    # load poses
    posetracker = PoseTracker.load(args.poses, Nimg, D, None, ind)

    # load ctf
    if args.ctf is not None:
        if args.use_real:
            raise NotImplementedError("Not implemented with real-space encoder. Use phase-flipped images instead")
        log('Loading ctf params from {}'.format(args.ctf))
        ctf_params = utils.load_pkl(args.ctf)
        if args.ind is not None: ctf_params = ctf_params[ind]
        if ctf_params.shape[1] == 7: # backwards compatibility with no parsing of phase shift
            ctf_params = np.concatenate([ctf_params,np.zeros((Nimg,1),dtype=np.float32)], axis=1)
        assert ctf_params.shape == (Nimg, 8)
        ctf.print_ctf_params(ctf_params[0])
        ctf_params = torch.tensor(ctf_params)
    else: ctf_params = None

    # instantiate model
    lattice = Lattice(D, extent=0.5)
    if args.enc_mask is None:
        args.enc_mask = D//2
    if args.enc_mask > 0:
        assert args.enc_mask <= D//2
        enc_mask = lattice.get_circular_mask(args.enc_mask)
        in_dim = enc_mask.sum()
    elif args.enc_mask == -1:
        enc_mask = None
        in_dim = lattice.D**2 if not args.use_real else (lattice.D-1)**2
    else: 
        raise RuntimeError("Invalid argument for encoder mask radius {}".format(args.enc_mask))
    model = HetOnlyVAE(lattice, args.qlayers, args.qdim, args.players, args.pdim,
                in_dim, args.zdim, encode_mode=args.encode_mode, enc_mask=enc_mask,
                enc_type=args.pe_type, domain=args.domain)

    log('Loading weights from {}'.format(args.weights))
    checkpoint = torch.load(args.weights)
    model.load_state_dict(checkpoint['model_state_dict'])

    model.eval()
    z_mu_all = []
    z_logvar_all = []
    gen_loss_accum = 0
    kld_accum = 0
    loss_accum = 0
    batch_it = 0
    data_generator = DataLoader(data, batch_size=args.batch_size, shuffle=False)
    for minibatch in data_generator:
        ind = minibatch[-1].to(device)
        y = minibatch[0].to(device)
        yt = minibatch[1].to(device) if tilt is not None else None
        B = len(ind)
        batch_it += B

        yr = torch.from_numpy(data.particles_real[ind]).to(device) if args.use_real else None
        rot, tran = posetracker.get_pose(ind)
        ctf_param = ctf_params[ind] if ctf_params is not None else None
    
        z_mu, z_logvar, loss, gen_loss, kld = eval_batch(model, lattice, y, yt, rot, tran, beta, tilt, ctf_params=ctf_param, yr=yr)
        
        z_mu_all.append(z_mu)
        z_logvar_all.append(z_logvar)

        # logging
        gen_loss_accum += gen_loss*B
        kld_accum += kld*B
        loss_accum += loss*B

        if batch_it % args.log_interval == 0:
            log('# [{}/{} images] gen loss={:.4f}, kld={:.4f}, beta={:.4f}, loss={:.4f}'.format(batch_it, Nimg, gen_loss, kld, beta, loss))
    log('# =====> Average gen loss = {:.6}, KLD = {:.6f}, total loss = {:.6f}'.format(gen_loss_accum/Nimg, kld_accum/Nimg, loss_accum/Nimg))

    z_mu_all = np.vstack(z_mu_all)
    z_logvar_all = np.vstack(z_logvar_all)
    
    with open(args.o,'wb') as f:
        pickle.dump(z_mu_all, f)
        pickle.dump(z_logvar_all, f)
        pickle.dump([loss_accum, gen_loss_accum, kld_accum], f)

    log('Finsihed in {}'.format(dt.now()-t1))

if __name__ == '__main__':
    args = parse_args().parse_args()
    utils._verbose = args.verbose
    main(args)

