"""简易负载测试（G-08）：python loadtest.py BASE_URL [TOKEN] [并发=20] [请求数=500]
只读接口（/livez、/readyz、/api/v1/auth/me、/api/v1/companies），输出吞吐与 p50/p95/p99。不要对生产在业务高峰运行。"""
import asyncio
import statistics
import sys
import time

import httpx


async def main(base, token, conc, total):
    paths = ["/livez", "/readyz"] + (["/api/v1/auth/me", "/api/v1/companies?size=20"] if token else [])
    h = {"Authorization": f"Bearer {token}"} if token else {}
    lat, errs, sem = [], 0, asyncio.Semaphore(conc)
    async with httpx.AsyncClient(base_url=base, headers=h, timeout=30) as c:
        async def one(i):
            nonlocal errs
            async with sem:
                t = time.perf_counter()
                try:
                    r = await c.get(paths[i % len(paths)])
                    if r.status_code >= 400:
                        errs += 1
                except httpx.HTTPError:
                    errs += 1
                lat.append((time.perf_counter() - t) * 1000)
        t0 = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(total)))
        dur = time.perf_counter() - t0
    q = statistics.quantiles(lat, n=100)
    print(f"requests={total} concurrency={conc} errors={errs} rps={total / dur:.1f} p50={q[49]:.1f}ms p95={q[94]:.1f}ms p99={q[98]:.1f}ms")
    return errs


if __name__ == "__main__":
    a = sys.argv[1:]
    sys.exit(1 if asyncio.run(main(a[0], a[1] if len(a) > 1 else "", int(a[2]) if len(a) > 2 else 20, int(a[3]) if len(a) > 3 else 500)) else 0)
