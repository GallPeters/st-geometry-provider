import sys, random, timeit
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from qgis.core import QgsApplication, QgsGeometry
app = QgsApplication([], False); app.initQgis()
from src import st_decoder as D

UX = 1.0e12          # arbitrary synthetic coordinate grid (units per degree)
FX = FY = -1000.0   # arbitrary synthetic false origin
UZ = 1000.0
def enc(v):
    neg = v < 0; v = abs(v); out = [(v & 0x3F) | (0x40 if neg else 0)]; v >>= 6
    while v: out[-1] |= 0x80; out.append(v & 0x7F); v >>= 7
    return bytes(out)
def blob(pts, zs=None, zmode="bd"):
    xi = [round((x - FX) * UX) for x, y in pts]; yi = [round((y - FY) * UX) for x, y in pts]
    def deltas(a): return [a[0]] + [a[i] - a[i - 1] for i in range(1, len(a))]
    dx, dy = deltas(xi), deltas(yi)
    if zs is None:
        vals = [v for pair in zip(dx, dy) for v in pair]
    else:
        zr = [round(z * UZ) for z in zs]
        dz = deltas(zr) if zmode[1] == "d" else zr
        if zmode[0] == "i":
            vals = [v for tr in zip(dx, dy, dz) for v in tr]
        else:
            vals = [v for pair in zip(dx, dy) for v in pair] + dz
    return b"\0" * 8 + b"".join(enc(v) for v in vals)

random.seed(11)
def rnd_ring(x, y, s, k):
    r = [(x + random.uniform(0, s), y + random.uniform(0, s)) for _ in range(k)]
    return r + [r[0]]

total_bad = 0
for zmode in ("bd", "id", "ba", "ia"):
    for keep_z in (False, True):
        dec = D.EsriDecoder(FX, FY, UX, 0.0, UZ, zmode, keep_z, (1, 4, 8))
        ents, npts, blobs = [], [], []
        for i in range(3000):
            x, y = 10 + random.random(), 45 + random.random()
            kind = random.random()
            use_z = random.random() < 0.7
            if kind < 0.6:                              # single-ring polygon
                pts = rnd_ring(x, y, 1e-3, random.randint(3, 30)); e = 8
            elif kind < 0.7:                            # two rings
                pts = rnd_ring(x, y, 1e-3, 5) + rnd_ring(x + 2e-3, y, 1e-3, 4); e = 8
            elif kind < 0.85:
                pts = [(x, y)]; e = 1
            else:
                pts = [(x + j * 1e-4, y + random.uniform(-1e-4, 1e-4)) for j in range(random.randint(2, 20))]; e = 4
            zs = [random.uniform(-50, 500) for _ in pts] if use_z else None
            b = blob(pts, zs, zmode)
            if random.random() < 0.01:
                b = b[:-2]                              # truncated -> must be rejected by both paths
            ents.append(e); npts.append(len(pts)); blobs.append(b)
        t0 = timeit.default_timer()
        got = dec.decode_batch(ents, npts, blobs)
        tb = timeit.default_timer() - t0
        t0 = timeit.default_timer()
        ref = [dec.geometry(e, n, None, b) for e, n, b in zip(ents, npts, blobs)]
        tr = timeit.default_timer() - t0
        bad = handled = 0
        for g, r in zip(got, ref):
            if g is None:
                continue
            handled += 1
            if r is None or bytes(g.asWkb()) != bytes(r.asWkb()):
                bad += 1
        total_bad += bad
        print("zmode=%s keep_z=%-5s batch handled %4d/3000  mismatches %d   batch %.1f us/row vs per-row %.1f us/row"
              % (zmode, keep_z, handled, bad, tb / 3000 * 1e6, tr / 3000 * 1e6))
print("TOTAL MISMATCHES:", total_bad)
app.exitQgis()
