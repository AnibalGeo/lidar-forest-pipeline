# Validation case study: dual-path stockpile volumes and a hidden DSM bias

**Context.** Airborne LiDAR block over managed forest, south-central Chile
(7 strips, 187 M points, ~192 pts/m²). One deliverable was the net volume of a
material stockpile inside the block. This note documents how a cross-check
between two independent volume methods surfaced a systematic bias in the digital
surface model (DSM), how the bias was traced to a single implicit parameter, and
how fixing it brought the two methods into agreement.

## The problem

A stockpile volume is only as trustworthy as the surface it is integrated from.
A single-method number — "grid the cloud, subtract a base, sum the heights" — has
no internal check: if the surface is biased, the volume is biased by the same
amount and nothing in the calculation complains.

So the volume was computed **two independent ways** against the same reference
DTM base:

- **(a) point-cloud path** — grid the classified cloud to the highest return per
  cell, integrate height above the base DTM.
- **(b) raster path** — take the already-produced 1 m DSM and integrate it above
  the same base DTM.

The two share no intermediate surface. If they agree, the volume is real. They
did not agree: the raster path (b) came out **+10.9 %** above the cloud path (a).
A 10.9 % disagreement on a volume deliverable is not method noise — one of the
two surfaces was wrong.

## The method: localize the disagreement

Rather than argue about which number to trust, the disagreement was mapped. The
DSM and the point-cloud surface were differenced cell by cell over the stockpile
polygon (7 594 valid 1 m cells) and the residual `DSM − cloud` was examined:

- minimum: 0.00 m
- mean: **+3.21 m**
- maximum: 34.25 m (along pile edges)

![DSM minus point-cloud surface, over the stockpile](figures/dsm_vs_laz_diff.png)

The residual is **one-signed** — the DSM is never below the cloud, only above,
by ~3 m on average across the whole pile and much more along the edges. A random
gridding difference would scatter around zero; a uniform positive offset is a
systematic fabrication. The DSM was inventing height.

## The cause: one implicit parameter

The DSM was produced with PDAL's `writers.gdal` using `output_type=max` and two
parameters left at their library defaults:

- **`radius` (unset → `resolution·√2` = 1.4142 m).** For a 1 m grid this pulls
  points from a 1.41 m circle, so every cell's "maximum" also sees returns
  belonging to its neighbours — including taller ones. On a stockpile with sharp
  edges and a rough surface, this ratchets the per-cell max upward.
- **`window_size=3` (moving-window gap fill).** Where a cell had no point, the
  writer filled it by averaging a 3-cell neighbourhood — fabricating coverage
  that no return supports, and doing so preferentially from the higher
  surroundings.

Neither default is *wrong* in general — both are sensible for a gap-tolerant
terrain model. They are wrong for a **surface** product that must represent only
real, cell-local returns. The defaults were never chosen; they were inherited.

The same implicit `radius` had already inflated the **density** raster by ~6×
(mean 1146 vs 288 counts/cell once corrected), which is what first drew attention
to the writer's defaults.

## The fix

The DSM (and density) writers were re-run with the radius and fill pinned
explicitly:

```yaml
dsm:
  output_type: max
  radius: 0.7071      # = resolution·√2 / 2, the circle that circumscribes the 1 m cell
  window_size: 0      # no gap fill: a cell with no return stays nodata
```

`radius=0.7071` is the smallest radius that still covers the whole 1 m cell
(the inscribed circle of 0.5 m would leave the corners uncovered), so real
coverage is preserved but neighbour bleed is minimized. `window_size=0` removes
the fabricated fill entirely.

Effect on the DSM:
- mean bias on shared cells: **+2.1 m → ~0**;
- a 171.7 m artefact spike (pure fill fabrication) removed from the derived CHM;
- 12 046 fabricated cells (from the moving-window fill) dropped.

## The result: convergence

Re-running both volume paths against the same base DTM:

| variant | net volume | vs (a) |
|---------|-----------:|-------:|
| (a) point cloud | 218 467 m³ | — |
| (b) DSM, uncorrected | 242 284 m³ | +10.9 % |
| (b) DSM, corrected | 222 889 m³ | **+2.02 %** |

The DSM path fell **8.0 %** and the two independent methods converged from a
10.9 % disagreement to **+2.02 %** — within the difference expected between a
point-cloud surface and a rasterized one. With the methods in agreement, the
218 467 m³ figure is validated, not merely computed.

## What generalizes

1. **Cross-check with a method that shares no intermediate.** The bias was
   invisible inside either single calculation; it only appeared as a disagreement
   between two. The +2.02 % residual is now a routine QC number, not an argument.
2. **Map disagreements, don't average them.** The cell-by-cell difference raster
   turned "the numbers are 10.9 % apart" into "the DSM is uniformly +3.2 m high,"
   which points straight at a cause.
3. **A one-signed residual is a systematic error.** Random method differences
   scatter around zero; a consistent offset is a fabrication with a source.
4. **Library defaults are not reproducibility.** The whole bias came from two
   parameters nobody chose. The pipeline now pins *every* writer parameter in
   config — including the ones that equal a default — so no deliverable can
   silently depend on a tool version's defaults again.
