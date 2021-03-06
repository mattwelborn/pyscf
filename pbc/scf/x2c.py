#!/usr/bin/env python


import time
from functools import reduce
import copy
import numpy
import scipy.linalg
from pyscf import lib
from pyscf.gto import mole
from pyscf.lib import logger
from pyscf.scf import x2c
from pyscf.pbc import gto as pbcgto
from pyscf.pbc.df import pwdf
from pyscf.pbc.df import pwdf_jk
from pyscf.pbc.df import ft_ao


def sfx2c1e(mf):
    '''Spin-free X2C.
    For the given SCF object, update the hcore constructor.

    Args:
        mf : an SCF object

    Returns:
        An SCF object

    Examples:

    >>> mol = gto.M(atom='H 0 0 0; F 0 0 1', basis='ccpvdz', verbose=0)
    >>> mf = scf.sfx2c1e(scf.RHF(mol))
    >>> mf.scf()

    >>> mol.symmetry = 1
    >>> mol.build(0, 0)
    >>> mf = scf.sfx2c1e(scf.UHF(mol))
    >>> mf.scf()
    '''
    mf_class = mf.__class__
    if mf_class.__doc__ is None:
        doc = ''
    else:
        doc = mf_class.__doc__
    class X2C_HF(mf_class):
        __doc__ = doc + \
        '''
        Attributes for spin-free X2C:
            with_x2c : X2C object
        '''
        def __init__(self):
            self.with_x2c = SpinFreeX2C(mf.mol)
            self.__dict__.update(mf.__dict__)
            self._keys = self._keys.union(['with_x2c'])

        def get_hcore(self, cell=None, kpts=None, kpt=None):
            if cell is None: cell = self.cell
            if kpts is None:
                if hasattr(self, 'kpts'):
                    kpts = self.kpts
                else:
                    if kpt is None:
                        kpts = self.kpt
                    else:
                        kpts = kpt
            if self.with_x2c:
                return self.with_x2c.get_hcore(cell, kpts)
            else:
                return mf_class.get_hcore(self, cell, kpts)

    return X2C_HF()

sfx2c = sfx2c1e

class X2C(x2c.X2C):
    def __init__(self, cell, kpts=None):
        self.exp_drop = 0.2
        self.approx = 'atom1e'
        self.xuncontract = True
        self.basis = None
        self.cell = self.mol = cell

class SpinFreeX2C(X2C):
    def get_hcore(self, cell=None, kpts=None):
        if cell is None: cell = self.cell
        if kpts is None:
            kpts_lst = numpy.zeros((1,3))
        else:
            kpts_lst = numpy.reshape(kpts, (-1,3))

        xcell, contr_coeff = self.get_xmol(cell)
        with_df = pwdf.PWDF(xcell)
        c = lib.param.LIGHT_SPEED
        assert('1E' in self.approx.upper())
        if 'ATOM' in self.approx.upper():
            atom_slices = xcell.offset_nr_by_atom()
            nao = xcell.nao_nr()
            x = numpy.zeros((nao,nao))
            vloc = numpy.zeros((nao,nao))
            wloc = numpy.zeros((nao,nao))
            for ia in range(xcell.natm):
                ish0, ish1, p0, p1 = atom_slices[ia]
                shls_slice = (ish0, ish1, ish0, ish1)
                t1 = xcell.intor('cint1e_kin_sph', shls_slice=shls_slice)
                v1 = xcell.intor('cint1e_nuc_sph', shls_slice=shls_slice)
                s1 = xcell.intor('cint1e_ovlp_sph', shls_slice=shls_slice)
                w1 = xcell.intor('cint1e_pnucp_sph', shls_slice=shls_slice)
                vloc[p0:p1,p0:p1] = v1
                wloc[p0:p1,p0:p1] = w1
                x[p0:p1,p0:p1] = x2c._x2c1e_xmatrix(t1, v1, w1, s1, c)
        else:
            raise NotImplementedError

        t = xcell.pbc_intor('cint1e_kin_sph', 1, lib.HERMITIAN, kpts_lst)
        s = xcell.pbc_intor('cint1e_ovlp_sph', 1, lib.HERMITIAN, kpts_lst)
        v = with_df.get_nuc(kpts_lst)
        #w = get_pnucp(with_df, kpts_lst)
        if self.basis is not None:
            s22 = s
            s21 = pbcgto.intor_cross('cint1e_ovlp_sph', xcell, cell, kpts=kpts_lst)

        h1_kpts = []
        for k in range(len(kpts_lst)):
            #h1 = x2c._get_hcore_fw(t[k], vloc, wloc, s[k], x, c) - vloc + v[k]
            #h1 = x2c._get_hcore_fw(t[k], v[k], w[k], s[k], x, c)
            h1 = x2c._get_hcore_fw(t[k], v[k], wloc, s[k], x, c)
            if self.basis is not None:
                c = lib.cho_solve(s22[k], s21[k])
                h1 = reduce(numpy.dot, (c.T, h1, c))
            if self.xuncontract and contr_coeff is not None:
                h1 = reduce(numpy.dot, (contr_coeff.T, h1, contr_coeff))
            h1_kpts.append(h1)

        if kpts is None or numpy.shape(kpts) == (3,):
            h1_kpts = h1_kpts[0]
        return lib.asarray(h1_kpts)


# We still use Ewald-like technique to compute spVsp.
# Theoratically, spVsp is not divergent because the numeriator spsp and the
# denorminator in Coulomb kernel 4pi/G^2 are cancelled.  A real space lattice
# sum can converge to a finite value.  However, it's difficult to accurately
# converge this value, large number of images in lattice summation is still
# required.
def get_pnucp(mydf, kpts=None):
    cell = mydf.cell
    if kpts is None:
        kpts_lst = numpy.zeros((1,3))
    else:
        kpts_lst = numpy.reshape(kpts, (-1,3))

    log = logger.Logger(mydf.stdout, mydf.verbose)
    t1 = t0 = (time.clock(), time.time())

    nkpts = len(kpts_lst)
    nao = cell.nao_nr()
    nao_pair = nao * (nao+1) // 2

    Gv, Gvbase, kws = cell.get_Gv_weights(mydf.gs)
    kpt_allow = numpy.zeros(3)
    if mydf.eta == 0:
        charge = -cell.atom_charges()
        #coulG=4*numpy.pi/G^2 is cancelled with (sigma dot p i, sigma dot p j)
        SI = cell.get_SI(Gv)
        vGR = numpy.einsum('i,ix->x', 4*numpy.pi*charge, SI.real) * kws
        vGI = numpy.einsum('i,ix->x', 4*numpy.pi*charge, SI.imag) * kws
        wjR = numpy.zeros((nkpts,nao_pair))
        wjI = numpy.zeros((nkpts,nao_pair))
    else:
        nuccell = copy.copy(cell)
        half_sph_norm = .5/numpy.sqrt(numpy.pi)
        norm = half_sph_norm/mole._gaussian_int(2, mydf.eta)
        chg_env = [mydf.eta, norm]
        ptr_eta = cell._env.size
        ptr_norm = ptr_eta + 1
        chg_bas = [[ia, 0, 1, 1, 0, ptr_eta, ptr_norm, 0] for ia in range(cell.natm)]
        nuccell._atm = cell._atm
        nuccell._bas = numpy.asarray(chg_bas, dtype=numpy.int32)
        nuccell._env = numpy.hstack((cell._env, chg_env))

        wj = lib.asarray(mydf._int_nuc_vloc(nuccell, kpts_lst, 'cint3c2e_pvp1_sph'))
        wjR = wj.real
        wjI = wj.imag
        t1 = log.timer_debug1('pnucp pass1: analytic int', *t1)

        charge = -cell.atom_charges()
        #coulG=4*numpy.pi/G^2 is cancelled with (sigma dot p i, sigma dot p j)
        aoaux = ft_ao.ft_ao(nuccell, Gv)
        vGR = numpy.einsum('i,xi->x', 4*numpy.pi*charge, aoaux.real) * kws
        vGI = numpy.einsum('i,xi->x', 4*numpy.pi*charge, aoaux.imag) * kws

    max_memory = max(2000, mydf.max_memory-lib.current_memory()[0])
    for k, pqkR, pqkI, p0, p1 \
            in mydf.ft_loop(mydf.gs, kpt_allow, kpts_lst,
                            max_memory=max_memory, aosym='s2'):
# rho_ij(G) nuc(-G) / G^2
# = [Re(rho_ij(G)) + Im(rho_ij(G))*1j] [Re(nuc(G)) - Im(nuc(G))*1j] / G^2
        if not pwdf_jk.gamma_point(kpts_lst[k]):
            wjI[k] += numpy.einsum('k,xk->x', vGR[p0:p1], pqkI)
            wjI[k] -= numpy.einsum('k,xk->x', vGI[p0:p1], pqkR)
        wjR[k] += numpy.einsum('k,xk->x', vGR[p0:p1], pqkR)
        wjR[k] += numpy.einsum('k,xk->x', vGI[p0:p1], pqkI)
    t1 = log.timer_debug1('contracting Vnuc', *t1)

    if mydf.eta != 0 and cell.dimension == 3:
        nucbar = sum([z/nuccell.bas_exp(i)[0] for i,z in enumerate(charge)])
        nucbar *= numpy.pi/cell.vol * 2
        ovlp = cell.pbc_intor('cint1e_kin_sph', 1, lib.HERMITIAN, kpts_lst)
        for k in range(nkpts):
            s = lib.pack_tril(ovlp[k])
            wjR[k] -= nucbar * s.real
            wjI[k] -= nucbar * s.imag

    wj = []
    for k, kpt in enumerate(kpts_lst):
        if pwdf_jk.gamma_point(kpt):
            wj.append(lib.unpack_tril(wjR[k]))
        else:
            wj.append(lib.unpack_tril(wjR[k]+wjI[k]*1j))

    if kpts is None or numpy.shape(kpts) == (3,):
        wj = wj[0]
    return wj


if __name__ == '__main__':
    from pyscf.pbc import scf
    cell = pbcgto.Cell()
    cell.build(unit = 'B',
               a = numpy.eye(3)*4,
               gs = [5]*3,
               atom = 'H 0 0 0; H 0 0 1.8',
               verbose = 4,
               basis='sto3g')
    lib.param.LIGHT_SPEED = 2
    mf = scf.RHF(cell)
    mf.with_df = pwdf.PWDF(cell)
    enr = mf.kernel()
    print('E(NR) = %.12g' % enr)

    mf = sfx2c1e(mf)
    esfx2c = mf.kernel()
    print('E(SFX2C1E) = %.12g' % esfx2c)

    mf = scf.KRHF(cell)
    mf.with_df = pwdf.PWDF(cell)
    mf.kpts = cell.make_kpts([2,2,1])
    enr = mf.kernel()
    print('E(k-NR) = %.12g' % enr)

    mf = sfx2c1e(mf)
    esfx2c = mf.kernel()
    print('E(k-SFX2C1E) = %.12g' % esfx2c)
