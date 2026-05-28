# LadybugDB Bug Report Draft

**Title**: Buffer manager crashes on kernels with `CONFIG_PAGE_SIZE_16KB=y` (e.g., Raspberry Pi 5 aarch64)

**Repo**: https://github.com/LadybugDB/ladybug  
**Affected version**: 0.16.1  
**Platform**: aarch64 Linux, Raspberry Pi 5 (`linux-aarch64-manylinux_2_26`)  
**Python**: 3.13  

---

## Summary

LadybugDB 0.16.1 crashes at runtime on kernels built with `CONFIG_PAGE_SIZE_16KB=y` (e.g., the official Raspberry Pi OS kernel for RPi5). The buffer manager calls `madvise(addr, 4096, MADV_DONTNEED)` with 4 KB alignment, but on 16 KB page kernels `madvise` returns `EINVAL` for any address or length not aligned to the kernel page size (16 KB). This causes a fatal `RuntimeError` after approximately 10,000–15,000 write operations.

---

## System Information

```
OS:     Raspberry Pi OS (Debian bookworm), aarch64
Kernel: 6.18.29+rpt-rpi-2712 (official RPi kernel)
Python: 3.13
Package: ladybug==0.16.1
         ladybug-0.16.1-cp313-cp313-manylinux_2_26_aarch64.manylinux_2_28_aarch64.whl
```

**Key kernel configuration**:
```
CONFIG_PAGE_SIZE_16KB=y   # confirmed via /boot/config-$(uname -r)
```

```
$ getconf PAGE_SIZE
16384
```

---

## Reproduction

```python
import ladybug as lb

db   = lb.Database("/tmp/test.lbug", buffer_pool_size=256*1024*1024)
conn = lb.Connection(db)
conn.execute("CREATE NODE TABLE T(id STRING, PRIMARY KEY(id))")

# Fails at approximately i=12,000–15,000 (varies with node data size)
for i in range(20000):
    conn.execute(f"MERGE (:T {{id: 'node_{i}_{'x'*100}'}})")
```

**Error**:
```
RuntimeError: Buffer manager exception:
  Releasing physical memory associated with a frame failed with error code -1: Invalid argument.
```

---

## Root Cause

The buffer manager allocates its pool as:
```
mmap(NULL, 1073741824, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS|MAP_NORESERVE, -1, 0)
```

When evicting a 4 KB frame, it calls:
```
madvise(frame_addr, 4096, MADV_DONTNEED)  → EINVAL on 16KB-page kernels
```

On Linux with `CONFIG_PAGE_SIZE_16KB=y`, the kernel page size is 16384 bytes. The `madvise(2)` syscall returns `EINVAL` when the address is not aligned to, or the length is not a multiple of, the kernel page size.

**Verification**:
```python
import ctypes, mmap

libc = ctypes.CDLL("libc.so.6", use_errno=True)
m = mmap.mmap(-1, 4096)  # anonymous private
buf = (ctypes.c_char * 4096).from_buffer(m)
addr = ctypes.addressof(buf)

ctypes.set_errno(0)
ret = libc.madvise(ctypes.c_void_p(addr), ctypes.c_size_t(4096), ctypes.c_int(4))  # MADV_DONTNEED=4
print(ret, ctypes.get_errno())  # -1  22  (EINVAL)

# But 16KB-aligned madvise succeeds:
m2 = mmap.mmap(-1, 16384)
buf2 = (ctypes.c_char * 16384).from_buffer(m2)
addr2 = ctypes.addressof(buf2)
ctypes.set_errno(0)
ret2 = libc.madvise(ctypes.c_void_p(addr2), ctypes.c_size_t(16384), ctypes.c_int(4))
print(ret2, ctypes.get_errno())  # 0  0  (Success)
```

---

## Pattern of Failure

`strace -e madvise` shows LadybugDB calling madvise with 4096-byte frames:
```
madvise(0x7ffedbc51000, 4096, MADV_DONTNEED) = -1 EINVAL (Invalid argument)
```

The failure occurs consistently at a specific offset into the buffer pool (frame ~1025). This is the point at which the pool fills up and the first eviction is attempted.

---

## Workaround

**KùzuDB 0.11.3** (the previous name of LadybugDB) does not exhibit this issue — its buffer manager handles the madvise failure differently and continues without crashing.

An `LD_PRELOAD` shim that converts 16 KB-misaligned `MADV_DONTNEED`/`MADV_FREE` to no-ops suppresses the exception, but LadybugDB 0.16.1 then hits a secondary error:
```
RuntimeError: Buffer manager exception: Unable to allocate memory! The buffer pool is full and no memory could be freed!
```
This means the no-op approach does not work for 0.16.1 (the pool must actually release frames).

---

## Suggested Fix

In the buffer manager's frame-release code, round the `madvise` call up to the kernel page granule:

```cpp
// Before:
madvise(frame_addr, KUZU_PAGE_SIZE, MADV_DONTNEED);

// After (pseudocode):
size_t kernel_page = sysconf(_SC_PAGE_SIZE);
uintptr_t aligned_addr = (uintptr_t)frame_addr & ~(kernel_page - 1);
size_t aligned_len = ((KUZU_PAGE_SIZE + kernel_page - 1) / kernel_page) * kernel_page;
madvise((void*)aligned_addr, aligned_len, MADV_DONTNEED);
```

Alternatively, if `KUZU_PAGE_SIZE < sysconf(_SC_PAGE_SIZE)`, skip the `madvise` call or adjust the internal page size to match the kernel page granule at startup.

---

## Impact

All LadybugDB users on Raspberry Pi 4/5 (official RPi OS kernel), and any Linux system with `CONFIG_PAGE_SIZE_16KB=y` or `CONFIG_PAGE_SIZE_64KB=y`.

Raspberry Pi 5 is increasingly popular for AI/ML edge deployments. The RPi5 official kernel uses 16 KB pages for performance reasons (aligned with Apple Silicon and other modern aarch64 SoCs).
