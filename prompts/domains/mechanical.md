# Domain card: mechanical engineering

Applies to mechanical design, structural analysis, manufacturing, dynamics, tribology, fatigue/fracture, thermal-fluid and robot/mechanism design. The six HPE dimensions remain the scoring frame; map technical_soundness = mechanics/model validity, evidence_strength = test/simulation validation, reproducibility = geometry/material/boundary traceability, significance = design or physical insight.

## Required chain of evidence

A mechanical claim should connect:

**requirements / load cases → concept and geometry → material and manufacturing → model or test → performance / failure mode → safety and uncertainty**

A simulation result without boundary conditions, material model and validation is not evidence of physical behavior.

## What counts as evidence

- **Loads and boundary conditions**: load cases, constraints, contact, preload, thermal environment, gravity, tolerance stack-up; worst-case and nominal cases.
- **Model validation**: mesh independence, element type, solver settings, convergence, comparison with analytical limit or experiment; not only a contour plot.
- **Material model**: elastic/plastic parameters, hardening, fatigue curve, anisotropy, temperature dependence, source and scatter.
- **Testing**: specimen/bench geometry, standard, sensor calibration, sampling rate, repeat count, environmental control, uncertainty.
- **Manufacturing**: process capability, residual stress, surface finish, defects, assembly variation; design-for-manufacture implications.
- **Safety**: factor of safety basis, failure mode, redundancy, compliance with relevant standard, human/operational risk.
- **Optimization/ML**: objective, constraints, training/validation split, physical feasibility of optimized designs.

## Common loopholes

1. **Boundary-condition fantasy**: fixed/pinned/insulated assumptions that do not match the real assembly.
2. **Mesh/convergence neglect**: single mesh, no convergence study, singular stress at a re-entrant corner reported as real.
3. **Material-model mismatch**: room-temperature properties used for high-temperature or cyclic loading; no source/scatter.
4. **Validation gap**: simulation only, no experiment or analytical check; one load case generalized to all.
5. **Load-case cherry-picking**: only the favorable load case; no worst case, fatigue or accidental load.
6. **Fatigue/fracture shortcut**: static strength used to claim infinite life; no S-N/ε-N, mean stress, surface or size correction.
7. **Tolerance/assembly idealization**: nominal CAD geometry, no stack-up, no manufacturing variation.
8. **Safety-factor hand-waving**: "conservative" without a basis; no failure-mode analysis.
9. **Optimization overclaim**: objective improved while constraints violated or physical feasibility untested.
10. **Units/dimensions**: unit inconsistency or unstated normalization in formulas/plots.

## Review focus

- Separate **innovation** (new mechanism/design concept) from **technical highlight** (new model, test or manufacturing insight).
- For every major issue, cite the exact figure/table/page and name the missing load case, validation, standard or uncertainty.
- Do not demand AI-style benchmark statistics for a design paper; demand physical validation, safety basis and reproducibility of geometry/material/boundary conditions.
- For a review paper, assess coverage of mechanisms, design methods and failure modes, not "no new device".
