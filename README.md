# PSC Deficiency Analytics — Reproducing a GRA / TOPSIS / Three-Sigma Model for Concentrated Inspection Campaigns

A reproduction of a published decision-making model for optimizing **Concentrated Inspection Campaigns (CIC)**, using real **Port State Control (PSC)** inspection data from the **Paris MoU**.

The project downloads raw inspection data through the Paris MoU API, streams and parses large XML files, builds a **ship type × deficiency code** frequency matrix, and then applies three methods:

- **Grey Relational Analysis (GRA)** — ranks ship types
- **TOPSIS** — ranks ship types
- **Three-Sigma rule** — flags deficiency codes with unusually high frequency, as candidate CIC inspection items

---

## Table of Contents

1. [Background](#background)
2. [Research Question](#research-question)
3. [Tech Stack](#tech-stack)
4. [Quick Start](#quick-start)
5. [Architecture & Workflow](#architecture--workflow)
6. [Data](#data)
7. [Data Pipeline (`data.py`)](#data-pipeline-datapy)
8. [Analysis (`notebook.ipynb`)](#analysis-notebookipynb)
9. [Algorithms](#algorithms)
10. [Interpreting the Results](#interpreting-the-results)
11. [Design Decisions](#design-decisions)
12. [Limitations & Future Work](#limitations--future-work)

---

## Background

**Port State Control (PSC)** is the inspection of foreign ships in national ports. Its goal is to detect **deficiencies**: violations or conditions that put the ship, crew, cargo or environment at risk.

The **Paris MoU** is one of the main regional PSC agreements. Besides routine inspections, it runs an annual **Concentrated Inspection Campaign (CIC)**, a three-month campaign that focuses on one specific topic.

Traditional CIC topic selection usually looks at a **single deficiency series** from the **previous year**. This can miss important deficiency codes and react slowly to new trends. The reproduced paper proposes a data-driven alternative that combines **GRA**, **TOPSIS** and the **Three-Sigma rule** on historical PSC data.

According to the paper, the Paris MoU data contains **21 ship types** and **496 deficiency codes** over a 36-month window (Nov 2018 – Oct 2021).

---

## Research Question

> Based on the historical frequency of deficiencies per ship type, how can we prioritize:
> **(a)** the ship types that tend to show more, or more serious, problems, and
> **(b)** the deficiency codes that should be the focus of a CIC, reducing the "blind spots" of single-topic CIC selection?

---

## Tech Stack

| Purpose | Tools |
|---------|-------|
| Data acquisition | `requests`, Paris MoU file-server API |
| Config / secrets | `python-dotenv` |
| XML parsing | `xml.etree.ElementTree.iterparse` (streaming) |
| Data processing | pandas, NumPy |
| Normalization | scikit-learn `MinMaxScaler` |
| Visualization | Matplotlib / Seaborn |
| Environment | Python 3, Jupyter |

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Add your Paris MoU API key

Create a `.env` file in the project root:

```
PARISMOU_API_KEY=your_api_key_here
```

> Never commit `.env`. Add it to `.gitignore`.

### 3. Build the frequency matrix

```bash
python data.py
```

This downloads, unzips and parses the inspection files, then writes:
- a **sparse CSV** with `ship_type, deficiency_code, count` triples
- a **pivot CSV**: the `ship_type × deficiency_code` matrix

### 4. Run the analysis

```bash
jupyter notebook notebook.ipynb
```

---

## Architecture & Workflow

The project separates **data engineering** from **analysis**:

| File | Responsibility |
|------|----------------|
| `data.py` | API client, download, unzip, XML streaming parser, aggregation, CSV export |
| `notebook.ipynb` | Descriptive statistics, GRA, TOPSIS, Three-Sigma, visualizations |

```
Paris MoU API
     │  (1) authenticate + download ZIPs (cached locally)
     ▼
  ZIP files ──(2) unzip + recursive search for *.xml
     │
     ▼
  XML files ──(3) iterparse <Inspection> → extract (ship_type, deficiency_code)
     │
     ▼
  Counters ──(4) aggregate frequencies
     │
     ├──► sparse.csv   (ship_type, deficiency_code, count)
     └──► matrix.csv   (ship_type × deficiency_code)
                │
                ▼  (5) notebook.ipynb
     ┌──────────┼───────────────┐
     ▼          ▼               ▼
    GRA       TOPSIS       Three-Sigma
 (rank ship types)      (flag deficiency codes)
     └────┬─────┘               │
          ▼                     ▼
   GRA vs TOPSIS         candidate CIC items
   comparison            + heatmap vs ship types
```

---

## Data

### Source

PSC inspection records for **2018–2021** from the **Paris MoU THETIS-PSC** file server, accessed through its API.

### Key variables

| Variable | Description | XML location |
|----------|-------------|--------------|
| **Ship Type** | Category of the ship (e.g. bulk carrier, general cargo/multipurpose) | `Inspection → ShipParticulars → ShipType` (usually the `Value` attribute) |
| **Deficiency Code** | Code of the specific inspection item that failed (e.g. `07105`, `15150`), grouped in series such as `01000`, `07000`, `15000` | Inside `Deficiency` elements (attribute, text or nested tag) |

### Frequency matrix

The algorithms work on a **decision matrix**:

- **Rows (m):** ship types → the *alternatives*
- **Columns (n):** deficiency codes → the *criteria*
- **Values:** number of times each code was recorded for each ship type

```
                07105   15150   01000   ...
bulk carrier      412     389      77   ...
general cargo     501     430      95   ...
...
```
*(values are illustrative)*

---

## Data Pipeline (`data.py`)

### 1. API client — `ParisMouClient`

- Gets an **authorization token** (`getauthorizationtoken`) using the API key
- **Caches and refreshes** the token, which avoids extra calls and failed downloads after expiry
- **Skips files that already exist locally**, so repeated runs are fast

### 2. Unzip & XML discovery

Each ZIP is extracted to its own folder, and `.xml` files are found **recursively**. The pipeline does not depend on a fixed folder layout inside the archive.

### 3. Streaming XML parsing

```python
for event, elem in ET.iterparse(path, events=("end",)):
    if elem.tag == "Inspection":
        ...            # extract ship type + deficiency codes
        elem.clear()   # free memory
```

- `iterparse` processes the file **as a stream** instead of loading it all into memory.
- `elem.clear()` after each `<Inspection>` releases memory and prevents memory blow-up on very large files.

### 4. Robust field extraction

**`extract_ship_type`**
1. Reads the `Value` attribute of `ShipType`
2. Falls back to the element's text
3. Returns `None` if nothing is found, so the record is skipped and no wrong pair is created

**`extract_deficiency_code`** — several rules to survive schema drift and namespaces:
1. Checks `Deficiency` attributes whose name contains `code` or equals `Value`
2. Checks descendant tags whose name contains `code`, using their text first and then their `Value`
3. Falls back to a `Value` attribute on the `Deficiency` element itself

### 5. Aggregation

| Counter | Meaning |
|---------|---------|
| `pair_counts[(ship_type, code)]` | Occurrences of each code for each ship type |
| `shiptype_counter` | Inspections per ship type |
| `defcode_counter` | Total occurrences per deficiency code |
| `inspections_total` | Inspections read |
| `inspections_with_def` | Inspections with at least one deficiency |

Output: a **sparse CSV** of triples and a **pivot CSV** built with `fill_value=0`.

---

## Analysis (`notebook.ipynb`)

1. **Load** the matrix as a pandas DataFrame (`ship_type` + one column per deficiency code)
2. **Descriptive view:** bar chart of total deficiencies per ship type, to spot dominant types
3. **Set up the decision problem:**
   - `A` = vector of alternatives (ship types)
   - `E` = numeric `m × n` frequency matrix, cast to `float` where needed
4. Run **GRA**, **TOPSIS** and **Three-Sigma**
5. **Compare** the GRA and TOPSIS rankings (merge on ship type, rank comparison, scatter plot)
6. **Heatmap** of the selected high-frequency codes against ship types

---

## Algorithms

All criteria are **benefit-type**: a higher count means a higher inspection priority.

### Grey Relational Analysis (GRA)

GRA comes from Grey System Theory. It measures how close each *comparative sequence* (a ship type) is to a *reference sequence* when information is incomplete or uncertain.

| Step | Formula |
|------|---------|
| 1. Min-Max normalization per criterion | `x_i(k) ∈ [0, 1]` |
| 2. Reference sequence (ideal = max per criterion) | `x_0(k) = max_i x_i(k)` |
| 3. Absolute deviation | `Δ_i(k) = |x_0(k) − x_i(k)|` |
| 4. Grey relational coefficient (ξ = 0.5) | `γ_i(k) = (Δ_min + ξ·Δ_max) / (Δ_i(k) + ξ·Δ_max)` |
| 5. Grey relational grade | `Γ_i = mean_k γ_i(k)` |

**Output:** one grade per ship type, sorted in descending order (`finalGRA`).

### TOPSIS

TOPSIS picks the alternative that is **closest to the positive ideal** and **farthest from the negative ideal**.

| Step | Formula |
|------|---------|
| 1. Vector normalization | `r_ik = x_ik / sqrt(Σ_i x_ik²)` |
| 2. Ideal solutions | `best_k = max_i r_ik`, `worst_k = min_i r_ik` |
| 3. Euclidean distances | `S_i⁺ = sqrt(Σ_k (r_ik − best_k)²)`, `S_i⁻ = sqrt(Σ_k (r_ik − worst_k)²)` |
| 4. Closeness coefficient | `C_i = S_i⁻ / (S_i⁺ + S_i⁻)` |

- `C_i ≈ 1` means close to the ideal: high deficiency frequencies across many codes, so **high priority**
- `C_i ≈ 0` means close to the worst: low priority
- Columns whose divisor is zero use `1` instead, to avoid division by zero
- **No criterion weights** are used (all codes weigh the same), to reproduce the core logic of the paper

**Output:** a DataFrame with `S_best`, `S_worst`, `C`, sorted by `C`, plus a horizontal bar chart.

### Three-Sigma Rule

This rule flags deficiency codes that occur **far more often than average**.

1. Total occurrences per code: `SUM_k = Σ_i E_ik`
2. Mean `μ` and sample standard deviation `σ` of `SUM_k` (divided by `n − 1 = 495`)
3. A code is **significant** if `SUM_k > μ + 3σ`

The notebook also computes **4σ, 5σ and 6σ** thresholds. Raising the threshold shrinks the list of top codes to a set that is practical for a campaign.

Strict normality is not required. The rule is used as a simple, conservative **operational** cut-off: codes this frequent are the ones PSC officers check most often, so they make strong CIC candidates.

### Worked example (GRA)

3 ship types × 4 codes:

```
A1: [10, 0, 2, 8]
A2: [ 3, 1, 0, 4]
A3: [ 7, 2, 1, 1]
```

1. Min-Max normalize each column to [0, 1]
2. Reference sequence = column max → `[1, 1, 1, 1]`
3. Compute `Δ_i(k)`, then `Δ_min` and `Δ_max` over the whole matrix
4. Compute `γ_i(k)` with ξ = 0.5 and average per row → grade and ranking

The real dataset scales this to **21 × 496**.

---

## Interpreting the Results

| Method | Answers | Output |
|--------|---------|--------|
| **GRA** | Which ship types are most strongly related to the overall "high-deficiency" profile? | Ranking of ship types |
| **TOPSIS** | Which ship types are closest to the ideal high-deficiency profile, criterion by criterion? | Ranking of ship types |
| **Three-Sigma** | Which deficiency codes are exceptionally frequent? | Candidate CIC inspection items |

- GRA and TOPSIS give **complementary** views. Comparing their ranks shows whether the top ship types stay consistently high under both methods. The paper reports this for **general cargo/multipurpose** and **bulk carrier**.
- Codes above the 3σ threshold are **candidate CIC items or series**. The paper suggests a **three-year rolling window** to avoid the weaknesses of single-series, previous-year selection.

---

## Design Decisions

- **Streaming XML** (`iterparse` + `clear`) keeps memory usage stable on very large files.
- **Defensive extraction** of ship types and codes handles schema variations without dropping records.
- **Local caching** of downloads and API tokens makes repeated runs fast and avoids hitting the API.
- **Secrets in `.env`** means no hard-coded API keys.
- **Separation of concerns:** the data pipeline (`data.py`) is reusable apart from the analysis (`notebook.ipynb`).

---

## Limitations & Future Work

**Limitations**
- Only two dimensions are used: ship type and deficiency code.
- TOPSIS uses equal weights for all criteria.
- The full **rolling-window CIC mechanism** from the paper is not yet implemented.
- Results reflect the 2018–2021 period of the downloaded files.

**Next steps**
- Add more dimensions: **flag state**, **port**, **inspecting authority**, **time trends**
- Implement the **three-year rolling CIC** selection end to end
- Experiment with **criterion weights**, for example by deficiency severity or detention-related codes
- Package the pipeline as a CLI or scheduled job for yearly updates

---

## Reference

This project reproduces the model proposed in:
> *[Add the full citation of the original paper here: authors, title, journal, year, DOI]*
