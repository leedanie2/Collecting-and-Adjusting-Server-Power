# Original technical paper — correction notes

[draft.pdf](draft.pdf) and [draft.odt](draft.odt) are historical manuscript
snapshots. The [poster](../Poster/README.md) and
[POSTER_DATA.md](../../Code/grid-simulation/POSTER_DATA.md) supersede their
grid-result interpretation after correcting startup and operating-point errors.
Page numbers below refer to the printed page labels in the PDF.

| Location | Historical claim | Current interpretation |
|---|---|---|
| Grid Level Impact, pp. 33–34; Table 19 | CV changes −68.8%, +0.6%, +15.4%; only ramp.c reduces grid variability | Corrected CV reductions are 91.4 ± 1.4%, 8.2 ± 3.5%, 21.2 ± 12.7%, respectively |
| Figure 21 and Table 22, pp. 34–35 | Original Simulink grid scores and rankings | Use the corrected tables under `Code/grid-simulation/data/summary/rescored/`; original grid scores retain model artifacts |
| Conclusion, p. 36 | Only one method is effective; the reactive methods provide no significant benefit | All three reduce mean grid CV in the corrected analysis. The governor has substantial run/window variability; this is not a new statistical-significance claim |
| Conclusion, p. 36 | CPU conclusions extend to GPUs | The measured experiments are on one CPU server; GPU applicability requires separate measurements |

The poster uses a 30 s warm-up, scores ramp & fill on its workload plateau,
and sets the frequency operating point per trace. It reports voltage as the
limiting quantity in the modeled scenario, rather than evidence of physical
damage to an actual grid.

Table 20's **full-trace** runtime and CPU-energy costs remain applicable,
including ramp flanks. Table 23's separate detector evaluation also remains
applicable: 8/24 transitions detected, 33% recall, 19% precision, and reported
median lead −0.23 s, with ±0.5 s phase-anchor uncertainty. The experiment did
not establish reliable prediction ahead of transitions.

The PDF and ODT are preserved unchanged. These notes should accompany them
when discussing the current findings.
