# Sources — Grid Resiliency Metrics & Standards

Research compiled to support normalization of simulation risk metrics against real-world
industry thresholds. Organized by topic.

---

## Regulatory Standards & NERC Documents

- [NERC Balancing and Frequency Control Reference Document](https://www.nerc.com/comm/RSTC_Reliability_Guidelines/Reference_Document_NERC_Balancing_and_Frequency_Control.pdf)
  Normal operating band 59.95–60.05 Hz; alert threshold 59.5 Hz (under-frequency) / 62.2 Hz (over-frequency); UFLS typically triggers below 59.3 Hz.

- [NERC Whitepaper: Characteristics and Risks of Emerging Large Loads](https://www.nerc.com/globalassets/who-we-are/standing-committees/rstc/whitepaper-characteristics-and-risks-of-emerging-large-loads.pdf)
  Identifies hyperscale data centers as a novel grid risk category; flags rapid ramp capability (hundreds of MW in seconds) as the core concern; calls for harmonized technical requirements at the point of interconnection.

- [NERC Incident Review: Large Load Loss](https://www.nerc.com/globalassets/our-work/reports/event-reports/incident_review_large_load_loss.pdf)
  Post-event analysis of voltage-sensitive load shedding events; informs reconnection ramp-rate coordination requirements.

- [NERC 2024 Long-Term Reliability Assessment](https://www.nerc.com/globalassets/our-work/assessments/2024-ltra_corrected_july_2025.pdf)
  Annual reliability outlook; source for LOLE 1-day-in-10-years planning standard and regional capacity margins.

- [NERC 2025 State of Reliability Overview](https://www.nerc.com/globalassets/programs/rapa/pa/nerc_sor_2025_overview.pdf)
  Current reliability posture; discusses emerging large load risks and frequency response adequacy.

- [NERC ELCC Report, September 2025](https://www.nerc.com/globalassets/our-work/reports/special-reports/elcc_report._september_2025.pdf)
  Effective Load Carrying Capability methodology; source for LOLE/LOLP/LOLH/EUE metric definitions and the 0.1 days/year adequacy threshold.

---

## IEEE & Interconnection Standards

- [IEEE 1547-2018 — Standard for Interconnection and Interoperability of Distributed Energy Resources](https://en.wikipedia.org/wiki/IEEE_1547)
  Default active power ramp rate: 300 seconds to full rated output (~0.33%/s). Voltage change limit: 3%/s at medium-voltage POI, 5%/s at low-voltage POI.

- [Dominion Energy DER Interconnection Parameters Manual (2024)](https://www.dominionenergy.com/-/media/content/large-business-services/pdfs/virginia/der-interconnection-parameters-manual.pdf)
  Utility implementation of IEEE 1547-2018 ramp settings; shows how numeric limits are applied in practice.

- [NREL Smart Inverters Applications in Power Systems (IEEE PES TR67)](https://www.nrel.gov/media/docs/libraries/grid/smart-inverters-applications-in-power-systems.pdf)
  Technical background on ramp rate controls, volt-watt and volt-var functions, and inverter-based resource performance under IEEE 1547-2018.

---

## Large Load Interconnection — Data Centers

- [Best Practices for Large Load Interconnections: A North American Perspective on Data Centers (arXiv 2025)](https://arxiv.org/pdf/2601.12686)
  Comparative study of large-load interconnection requirements across U.S. power entities. Key finding: Southern Company is the only US utility with an explicit numeric ramp cap — **20 MW/min** under normal operation. Documents convergence/divergence across ISOs/RTOs.

- [GridLab Report: Practical Guidance and Considerations for Large Load Interconnections (2025)](https://gridlab.org/wp-content/uploads/2025/03/GridLab-Report-Large-Loads-Interim-Report.pdf)
  Interim report on best practices; discusses segmentation, ride-through, ramp-rate limits, and coordination protocols for loads >75 MW.

- [Enhancing Grid Resilience for Giga-Watt Scale Data Centers Using High Voltage Circuit Breaker Operated Braking Resistors (arXiv 2024)](https://arxiv.org/pdf/2512.21295)
  Technical paper on hardware mitigation for rapid data center load changes; quantifies frequency deviation from sudden MW-scale load steps.

- [FERC Grapples With Surging Reliability and Interconnection Demands From Data Centers (Orrick, 2025)](https://www.orrick.com/en/Insights/2025/11/FERC-Grapples-With-Surging-Reliability-and-Interconnection-Demands-From-Data-Centers)
  Policy overview; FERC opened a formal docket in April 2025 to investigate load loss events at data centers.

- [NERC Tees Up Plan to Assess Grid Risks Associated with Data Centers (White & Case)](https://www.whitecase.com/insight-alert/nerc-tees-plan-assess-grid-risks-associated-data-centers)
  NERC working group formation and scope; ERCOT definition of large load (≥75 MW at a single POI).

- [How ISOs and RTOs Are Addressing Large Load Growth in 2025 (Yes Energy)](https://www.yesenergy.com/blog/how-isos-and-rtos-are-addressing-large-load-growth-in-2025)
  Survey of ISO/RTO policy responses; PJM collecting granular data center ramp behavior data since 2024 using NERC survey templates.

- [Solving Interconnection Bottlenecks with Data Center Load Flexibility (Renewable Energy World)](https://www.renewableenergyworld.com/power-grid/solving-interconnection-bottlenecks-with-data-center-load-flexibility/)
  Industry perspective on demand flexibility as a grid integration strategy.

- [Data Centers and the Grid: How Hyperscale Computing Is Reshaping Power Infrastructure (Power Magazine)](https://www.powermag.com/data-centers-and-the-grid-how-hyperscale-computing-is-reshaping-power-infrastructure/)
  Overview of grid impact from hyperscale growth; facilities >20 MW interconnect at transmission level with dedicated substations.

---

## Grid Resilience Metrics

- [Resilience Assessment and Planning in Power Distribution Systems (arXiv 2023)](https://arxiv.org/pdf/2308.07552)
  Taxonomy of reliability vs resilience metrics; defines SAIDI, SAIFI, LOLE, LOLP, LOLH, EENS, and EUE with mathematical formulations.

- [Exploring Reliability Standard Metrics in a Net Zero Transition (UK DESNZ, 2023)](https://assets.publishing.service.gov.uk/media/65e3a3323f694514a3035fbe/5-exploring-reliability-standard-metrics-in-net-zero-transition.pdf)
  Comparative analysis of LOLE, LOLH, EUE adequacy criteria across jurisdictions; argues for complementary metrics beyond the 1-in-10 standard as renewable penetration increases.

- [Frequency Instability Problems in North American Interconnections (OSTI, 2011)](https://www.osti.gov/servlets/purl/1556900)
  Historical analysis of frequency events; defines under-frequency thresholds and load shedding trigger levels used by NERC reliability coordinators.

---

## Key Numeric Thresholds Used in Simulation

| Metric | Value | Source |
|--------|-------|--------|
| Ramp rate limit (large load) | 20 MW/min (0.333 MW/s) | Southern Company; arXiv 2601.12686 |
| IEEE 1547 default ramp | 300 s to full output | IEEE 1547-2018 |
| Frequency normal band | 59.95 – 60.05 Hz | NERC BAL-001 |
| Under-frequency alert | < 59.5 Hz | NERC |
| UFLS trigger | < 59.3 Hz | NERC |
| Resource adequacy target | LOLE ≤ 0.1 days/year | NERC 1-in-10 standard |
| Large load definition (ERCOT) | ≥ 75 MW at single POI | NERC/ERCOT |

---

## Grid Stiffness — Weak/Islanded-Microgrid Scenario (2026-07-13 retune)

The swing-equation scenario in `simulation/readscript.m` was retuned from a
conventional regional grid (H = 6 s, S_base = 10 GW) to a weak/islanded,
high-renewable-penetration setting (H = 2.5 s, S_base = 50 MW). Rationale:
at 10 GW a ~5 MW facility transient is ~5×10⁻⁴ pu — frequency sits inert at
~59.995 Hz regardless of workload or smoothing, so nadir/ROCOF could not
distinguish a smoother from none. R = 0.05 and τ_gov = 20 s are kept at
standard governor values.

- **H ≈ 2–3 s (low-inertia operating range):**
  - Kundur, *Power System Stability and Control* (1994) — typical synchronous
    machine inertia constants span ~2–10 s; H = 2.5 s is the documented low
    end, characteristic of small hydro/gas/diesel units that anchor islanded
    systems.
  - Milano, Dörfler, Hug, Hill & Verbič, "Foundations and Challenges of
    Low-Inertia Systems," *PSCC 2018* — frames declining system inertia under
    converter-interfaced (renewable) generation; low-inertia systems operate
    at effective H of a few seconds or below.
  - Tielens & Van Hertem, "The relevance of inertia in power systems,"
    *Renewable and Sustainable Energy Reviews* 55 (2016) 999–1009 — quantifies
    inertia decline with renewable penetration and its nadir/ROCOF impact.
- **S_base = 50 MW (islanded/weak-grid hosting):** puts the ~5 MW facility at
  ~10% of system load — the "large load on a small system" regime motivating
  the study (cf. ERCOT's 75 MW large-load threshold above, scaled to an
  islanded microgrid context). Island and remote microgrids in the tens of MW
  are the standard setting of the islanded-operation literature (see Milano
  et al. 2018; IEEE 1547.4 islanded-system planning).

Effect: lifts freq nadir/ROCOF out of the inert ~59.995 Hz regime so the
frequency-side metrics become discriminative (numbers in
`results/*/metrics.json`). The LOLE proxy in
`analysis/grid_metrics.py` is RREI-based and does not consume S_base — it is
unaffected by this retune.
