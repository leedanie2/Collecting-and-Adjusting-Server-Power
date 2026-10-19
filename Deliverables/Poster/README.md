# Research poster

**Smoothing Synchronized Power Swings in HPC Clusters Using Software:
Measurement, Prediction, and Grid Simulation**

Daniel Lee (Grinnell College), Emily Doyle (Embry-Riddle Aeronautical University),
and Alexander Johnson (Georgia Institute of Technology).

- [PDF proof](APS-poster-Lee-48x36-v3-mlstyle_proof.pdf)
- [Editable PowerPoint, 48 × 36 inches](APS-poster-Lee-48x36-v3-mlstyle.pptx)
- [Poster results, assumptions, and data sources](../../Code/grid-simulation/POSTER_DATA.md)

The PPTX and PDF are unchanged copies of the supplied `Research/poster/` files.
`figures/` contains the supplied result charts: the HPL power trace
(`C_hero_steady.png`), benefit versus cost (`E_tradeoff.png`), and voltage swing
(`F_voltage.png`). These images are snapshots accompanying the poster.

The poster uses the October 2026 corrected grid analysis. Its numerical results
can be regenerated from the committed raw traces using
`Code/grid-simulation/analysis/rescore_warm.py`; the data guide above explains
how the reported averages and error bars are calculated. The earlier paper and
presentation files describe the original analysis.
