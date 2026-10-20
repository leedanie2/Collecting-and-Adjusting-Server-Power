# Historical presentations — correction notes

- [Final Power Presentation.pdf](Final%20Power%20Presentation.pdf)
- [Exec Power Presentiation.pdf](Exec%20Power%20Presentiation.pdf)

These presentations predate the corrected analysis in the
[October 2026 poster](../Poster/README.md). Their original files are preserved.
Slide numbers below are the labels printed on the slides.

| Presentation / slides | Historical wording | Current finding |
|---|---|---|
| Final, 22–24 | Ramp.c has unreasonable cost; the smoother has little impact; the governor has insignificant benefit | Ramp & fill reduces grid CV by 91.4 ± 1.4%, at +121.1% runtime and +161.0% CPU energy. The smoother reduces CV by 8.2 ± 3.5%; the governor by 21.2 ± 12.7% |
| Final, 25; Executive, 16 | “Inconclusive results” | The corrected study quantifies smoothing and its costs. The reactive reductions are smaller and the governor result varies substantially across runs and scoring windows |
| Both presentations' grid results | Original model outputs | Startup and the fixed operating point bias those outputs; use the corrected poster analysis |

The current interpretation is that ramp & fill provides strong smoothing at
substantial cost, while reactive control provides a smaller reduction with
little runtime overhead. Forecasting experiments did not establish reliable
advance warning. Voltage crosses the study threshold first in the modeled
fleet-size sweep.

See [POSTER_DATA.md](../../Code/grid-simulation/POSTER_DATA.md) for exact
numbers, uncertainty, scoring windows, and source files. The original runtime
and energy costs are distinct from the corrected grid metrics.
