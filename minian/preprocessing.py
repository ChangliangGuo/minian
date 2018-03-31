import numpy as np
import xarray as xr
import itertools as itt
import functools as fct
import cv2
from dask import delayed, compute
from dask.diagnostics import ProgressBar
from collections import OrderedDict
from scipy.ndimage import uniform_filter
from scipy.stats import zscore
from warnings import warn
from .utilities import scale_varr
from IPython.core.debugger import set_trace


class HashableDict(dict):
    def __hash__(self):
        return hash(frozenset(self.items()))


def corr_coeff_pixelwise(varray):
    if varray.sizes['frame'] % 2 > 0:
        varr = varray.isel(frame=slice(None, -1))
    else:
        varr = varray

    def corr(a, axis):
        return np.apply_along_axis(
            lambda t: np.corrcoef(np.split(t, 2)[0], np.split(t, 2)[1])[0, 1],
            axis, a)

    return varr.reduce(corr, dim='frame')


# def mask_movie_framewise(mov, mask, vals):
#     mov_re = mov.reshape((mov.shape[0], -1))
#     mask_re = mask.flatten()
#     mov_masked = mov_re.copy()
#     np.apply_along_axis(lambda f: np.place(f, mask_re, vals), 1, mov_masked)
#     return mov_masked.reshape(mov.shape)


# def zscore_xr(xarr, dim=None):
#     mean = xarr.mean(dim=dim)
#     std = xarr.std(dim=dim)
#     return (xarr - mean) / std


def detect_brightspot(varray, thres=None, window=50, step=10):
    print("detecting brightspot")
    spots = xr.DataArray(varray.sel(frame=0)).reset_coords(
        drop=True).astype(int)
    spots.values = np.zeros_like(spots.values)
    meanfm = varray.mean(dim='frame')
    for ih, ph in meanfm.rolling(height=window):
        if ih % step == 0:
            for iw, pw in ph.rolling(width=window):
                if (iw % step == 0 and pw.sizes['height'] == window
                        and pw.sizes['width'] == window):
                    mean_z = xr.apply_ufunc(zscore, pw)
                    if not thres:
                        cur_thres = -mean_z.min().values
                    else:
                        cur_thres = thres
                    spots.loc[{
                        'height': slice(ih - window + 1, ih),
                        'width': slice(iw - window + 1, iw)
                    }] += mean_z > cur_thres
                    print(
                        ("processing window at {:3d}, {:3d}"
                         " using threshold: {:03.2f}").format(
                             int(ih), int(iw), float(cur_thres)),
                        end='\r')
    print("\nbrightspot detection done")
    return spots


def detect_brightspot_perframe(varray, thres=0.95):
    print("creating parallel schedule")
    spots = []
    for fid, fm in varray.rolling(frame=1):
        sp = delayed(lambda f: f > f.quantile(thres, interpolation='lower'))(
            fm)
        spots.append(sp)
    with ProgressBar():
        print("detecting bright spots by frame")
        spots, = compute(spots)
    print("concatenating results")
    spots = xr.concat(spots, dim='frame')
    return spots


# def correct_dust(varray, dust):
#     mov_corr = varray.values
#     nz = np.nonzero(dust)
#     nz_tp = [(d0, d1) for d0, d1 in zip(nz[0], nz[1])]
#     for i in range(np.count_nonzero(dust)):
#         cur_dust = (nz[0][i], nz[1][i])
#         cur_sur = set(
#             itt.product(
#                 range(cur_dust[0] - 1, cur_dust[0] + 2),
#                 range(cur_dust[1] - 1, cur_dust[1] + 2))) - set(
#                     cur_dust) - set(nz_tp)
#         cur_sur = list(
#             filter(
#                 lambda d: 0 < d[0] < mov.shape[1] and 0 < d[1] < mov.shape[2],
#                 cur_sur))
#         if len(cur_sur) > 0:
#             sur_arr = np.empty((mov.shape[0], len(cur_sur)))
#             for si, sur in enumerate(cur_sur):
#                 sur_arr[:, si] = mov[:, sur[0], sur[1]]
#             mov_corr[:, cur_dust[0], cur_dust[1]] = np.mean(sur_arr, axis=1)
#         else:
#             print("unable to correct for point ({}, {})".format(
#                 cur_dust[0], cur_dust[1]))
#     return mov_corr


def correct_brightspot(varray, spots, window=2, spot_thres=10, inplace=True):
    print("correcting brightspot")
    if not spots.sum() > 0:
        print("no bright spots to be corrected, returning input")
        return varray
    if not inplace:
        varr_ds = varray.copy()
    else:
        varr_ds = varray
    spot_dim = spots.dims
    red_dim = tuple(set(varray.dims) - set(spot_dim))
    if len(spot_dim) > 2:
        spot_thres = 0
    brt = np.nonzero(spots.values > spot_thres)
    brt_list = [
        HashableDict((dm, int(spots.coords[dm][brt[idm][ib]].values))
                     for idm, dm in enumerate(spot_dim))
        for ib in range(len(brt[0]))
    ]
    sur_list = []
    for ibrt, brt_cord in enumerate(brt_list):
        cur_sur = [(dim, list(
            set(range(co - window, co + window + 1)).intersection(
                set(varr_ds.coords[dim].values.tolist()))))
                   for dim, co in brt_cord.items()]
        cur_sur_list = []
        for cord in itt.product(* [cord_rg[1] for cord_rg in cur_sur]):
            cur_sur_list.append(
                HashableDict((cur_sur[i][0], cord[i]) for i in range(
                    len(cord))))
        cur_sur = list(set(cur_sur_list) - set(brt_list))
        sur_list.append(cur_sur)
    for ibrt, cur_brt in enumerate(brt_list):
        print(
            "processing spot {:3d} of {:3d}".format(ibrt, len(brt_list)),
            end='\r')
        if len(sur_list[ibrt]) > 0:
            cur_sur = xr.DataArray(
                np.zeros((len(sur_list[ibrt]), ) + tuple(
                    [varr_ds.sizes[rd] for rd in red_dim])),
                dims=('sample', ) + red_dim,
                coords=dict({
                    'sample': range(len(sur_list[ibrt]))
                }, **{r: varr_ds.coords[r]
                      for r in red_dim}))
            for isamp, cord_samp in enumerate(sur_list[ibrt]):
                cur_sur.loc[{'sample': isamp}] = varr_ds.loc[cord_samp]
            varr_ds.loc[cur_brt] = cur_sur.mean(dim='sample')
        else:
            print("unable to correct for point {}, coordinates: {}".format(
                ibrt, cur_brt))
    print("\nbrightspot correction done")
    return varr_ds.rename(varray.name + "_DeSpotted")


def remove_background_old(varray, window=51):
    print("removing background")
    varr_ft = varray.astype(np.float32)
    for fid, fm in varr_ft.rolling(frame=1):
        print(
            "processing frame {} of {}".format(
                int(fid), int(varray.sizes['frame'])),
            end='\r')
        varr_ft.loc[{'frame': fid}] = fm - uniform_filter(fm, window)
    print("\nbackground removal done")
    return scale_varr(
        varr_ft.rename(varray.name + "_Filtered"), (0, 255),
        inplace=True).astype(
            np.uint8, copy=False)


def remove_background(varray, window=51):
    print("creating parallel schedule")
    varr_ft = varray.astype(np.float32)
    compute_list = []
    for fid, fm in varr_ft.rolling(frame=1):
        _ = delayed(remove_background_perframe)(fid, fm, varr_ft, window)
        compute_list.append(_)
    with ProgressBar():
        print("removing background")
        compute(compute_list)
    print("normalizing result")
    varr_ft = scale_varr(varr_ft, (0, 255)).astype(varray.dtype, copy=False)
    print("background removal done")
    return varr_ft.rename(varray.name + "_Filtered")


def remove_background_perframe(fid, fm, varr, window):
    f = fm - uniform_filter(fm, window)
    varr.loc[dict(frame=fid)] = f


def stripe_correction(varray, reduce_dim='height'):
    varr_sc = varray.astype(np.float32)
    mean1d = varray.mean(dim='frame').mean(dim=reduce_dim)
    varr_sc -= mean1d
    return scale_varr(
        varr_sc.rename(varray.name + "_Stripe_Corrected"), (0, 255),
        inplace=True).astype(
            np.uint8, copy=False)


def gaussian_blur(varray, ksize=(3, 3), sigmaX=0):
    return varray.groupby('frame').apply(
        lambda fm: cv2.GaussianBlur(fm.values, ksize, sigmaX))