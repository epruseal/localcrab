/* LD_PRELOAD: 16KB 미정렬 madvise(MADV_DONTNEED/FREE)를 noop으로 대체.
 *
 * 배경:
 *   RPi5 aarch64는 CONFIG_PAGE_SIZE_16KB=y 커널을 사용한다.
 *   KùzuDB/LadybugDB의 buffer manager는 4KB 단위로 madvise를 호출하는데,
 *   16KB 페이지 커널에서 4KB-미정렬 madvise는 EINVAL을 반환한다.
 *   이 라이브러리는 미정렬 madvise를 noop으로 대체해 충돌을 방지한다.
 *
 * 부작용:
 *   물리 메모리가 OS로 반환되지 않음 (buffer pool이 최대 크기까지 사용).
 *   마이그레이션처럼 1회성 작업에서는 무방하다.
 *   서비스 상시 운용 시에는 buffer_pool_size를 명시적으로 제한해야 한다.
 *
 * 빌드:
 *   gcc -shared -fPIC -O2 -o madv_noop.so scripts/madv_noop.c -ldl
 *
 * 사용:
 *   LD_PRELOAD=/path/to/madv_noop.so opencrab serve
 *   LD_PRELOAD=/path/to/madv_noop.so python scripts/migrate_graph_to_ladybug.py
 */

#define _GNU_SOURCE
#include <sys/mman.h>
#include <dlfcn.h>
#include <stddef.h>
#include <stdint.h>

static int (*real_madvise)(void *addr, size_t length, int advice) = NULL;

#define PAGE_16K 16384UL

int madvise(void *addr, size_t length, int advice) {
    if (!real_madvise)
        real_madvise = dlsym(RTLD_NEXT, "madvise");

    /* 16KB 미정렬 DONTNEED/FREE → noop */
    if ((advice == MADV_DONTNEED || advice == MADV_FREE) &&
        ((uintptr_t)addr % PAGE_16K != 0 || length % PAGE_16K != 0)) {
        return 0;
    }
    return real_madvise(addr, length, advice);
}
