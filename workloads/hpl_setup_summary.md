# HPL Benchmark Setup Summary
Download from https://www.netlib.org/benchmark/hpl/
---

## System Configuration

| Component | Details |
|---|---|
| OS | RHEL/CentOS (yum-based, no `module` system) |
| MPI | OpenMPI at `/usr/lib64/openmpi/bin/mpicc` |
| BLAS | OpenBLAS at `/usr/lib64/libopenblas.so` |
| HPL Version | 2.3 |
| Install Path | `/home/<user>/hpl-2.3` |
| Binary | `/home/<user>/hpl-2.3/bin/Linux/xhpl` |

**PATH addition (in `~/.bashrc`):**
```bash
export PATH=/usr/lib64/openmpi/bin:$PATH
```

---

## Make.Linux Configuration

File location: `/home/<user>/hpl-2.3/Make.Linux`

```makefile
SHELL        = /bin/sh
CD           = cd
CP           = cp
LN_S         = ln -s
MKDIR        = mkdir
RM           = /bin/rm -f
TOUCH        = touch

ARCH         = Linux

TOPdir       = /home/<user>/hpl-2.3
INCdir       = $(TOPdir)/include
BINdir       = $(TOPdir)/bin/$(ARCH)
LIBdir       = $(TOPdir)/lib/$(ARCH)
HPLlib       = $(LIBdir)/libhpl.a

MPdir        = /usr/lib64/openmpi
MPinc        = -I/usr/include/openmpi-x86_64
MPlib        = -L/usr/lib64/openmpi/lib -lmpi

LAdir        = /usr/lib64
LAlib        = -L$(LAdir) -lopenblas

HPL_INCLUDES = -I$(INCdir) -I$(INCdir)/$(ARCH) $(MPinc)
HPL_LIBS     = $(HPLlib) $(LAlib) $(MPlib)
HPL_OPTS     =
HPL_DEFS     = $(HPL_OPTS) -DHPL_CALL_CBLAS

CC           = mpicc
CCNOOPT      = $(HPL_DEFS) $(HPL_INCLUDES)
CCFLAGS      = $(HPL_DEFS) $(HPL_INCLUDES) -O3 -funroll-loops
LINKER       = mpicc
LINKFLAGS    = $(CCFLAGS)
ARCHIVER     = ar
ARFLAGS      = r
RANLIB       = echo
```

---

## Build Commands

```bash
cd /home/<user>/hpl-2.3
make arch=Linux

# If rebuilding from scratch:
make clean arch=Linux
make arch=Linux
```

---

## Running the Benchmark

```bash
cd /home/<user>/hpl-2.3/bin/Linux
mpirun -np 4 --oversubscribe ./xhpl
```

**Result:** 864/864 tests passed, 0 failures.

To capture results cleanly:
```bash
mpirun -np 4 --oversubscribe ./xhpl | tee results.txt
grep WR results.txt  # filters Gflops performance lines
```

The rightmost column in `WR` lines is the **Gflops score**.

---

## Tuning

Edit `/home/<user>/hpl-2.3/bin/Linux/HPL.dat` to adjust:
- `N` — problem size (larger = more accurate benchmark, longer runtime)
- `NB` — block size (tune for cache efficiency, typically 128-256)
- `P` x `Q` — process grid (must satisfy P*Q = number of MPI processes)

---

## Integration Notes for Simulation

- HPL is a **dense linear algebra benchmark** (LU factorization via DGEMM)
- It stresses **floating point throughput, memory bandwidth, and MPI communication**
- Relevant if your simulation uses: BLAS routines, distributed linear solves, or MPI collectives
- The `HPL.dat` input file controls problem scale — can be scripted to sweep parameters
- Output Gflops can be used as a **hardware performance baseline** to normalize simulation timing results
- MPI process count (`-np`) should match your simulation's parallelism for apples-to-apples comparison
