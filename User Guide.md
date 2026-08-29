# StationFlow — User Guide

Short intro into the input and outputs of the code

---

## 0. Order of Use

Run 'code-presolution.py' first, to output the standard metrics from the original graph. Then run 'code-post_solution.py' to obtain a side by side comparison between the original and improved graph

## 1. Input Datasets

Datasets 'node_simplified.csv', 'stn_coor_010326.csv' and 'origin_destination_train_202504.csv' are found in the resources folder.

### 1.1 `resources/202504/node_simplified.csv`
The full list of MRT/LRT stations in the network.

| Column | Contents |
|---|---|
| `PT_CODE` | Station code, e.g. `NS9/TE2` for an interchange, `EW18` for a single-line station |

This file defines which stations exist and is used to build the network

### 1.2 `resources/202504/origin_destination_train_202504.csv`
Real passenger trip data: how many people travelled from one station to another.

| Column | Contents |
|---|---|
| `ORIGIN_PT_CODE` | Starting station code |
| `DESTINATION_PT_CODE` | Ending station code |
| `TOTAL_TRIPS` | Number of trips recorded between that origin/destination pair |
| `TIME_PER_HOUR` | Hour of day the trips occurred (used for the throughput-over-time curve fit) |

Used to form the weighted graph and used for computation of eiegnvalues and eiegnvectors.

### 1.3 `resources/stn_coor_010326.csv`
Geographic coordinates for each station, used only for plotting the network on a map.

| Column | Contents |
|---|---|
| `station_code` | Station code (matches `PT_CODE` or one part of an interchange code) |
| `lat` | Latitude |
| `lon` | Longitude |

Used only in the graph plots
---

## 2. What the Script Builds From These Inputs

1. **Station index** — from `node_simplified.csv`.
2. **Unweighted adjacency matrix** 
3. **Weighted adjacency matrix** 
4. **Fiedler value & vector** — the network's algebraic connectivity ($\lambda_2$) and the
   corresponding eigenvector, computed from the weighted matrix's normalized Laplacian.
5. **Station coordinates** — from the coordinate file, used only for map plots.

---

## 3. Outputs

All outputs are written to `output/` (created automatically).

| File | Contents |
|---|---|
| `vulnerable_stations.png` | Bar chart of the 5 stations with the highest \|eigenvector\| value |
| `selected_stations_eigenvector_values.csv` | Eigenvector value for the 5 pre-selected target stations |
| `station_throughput_logistic.png` | Curve-fit of hourly passenger throughput at the target stations (AM/PM peak model) |
| `edge_disruption_analysis.csv` | Network impact of removing one specific edge |
| `station_disruption_analysis.csv` | Network impact of removing one specific station |
| `<LINE>_line_disruption_analysis.csv` | Network impact of removing an entire line |
| `random_disruption_analysis.csv` | Network impact of removing a fixed number of random edges |
| `monte_carlo_disruption_summary.csv` | Per-station results across many randomized, vulnerability-weighted disruption trials |
| `network_expansion_analysis.csv` | Scored candidate new edges (nearby unconnected station pairs) |
| `network_original.png` | Map of the current network |
| `network_top_candidate.png` | Map with the best-scoring candidate new edge highlighted |
| `pareto_frontier.png` | Cost vs. satisfaction trade-off plot across sampled capacity/frequency combinations |
| Console output | Printed Fiedler value, top-10 candidate edges by composite score, and the selected optimal (capacity, frequency) point |

---

## 4. Benchmark Addition (`compute_lambda2_benchmark`)

An extra function that estimates what $\lambda_2$
"should" look like for a network of the same size and degree sequence.

**Inputs required:** `unweighted_adj_matrix`, `od_data_arr`, `node2index` — produced from the code already

**Output:**
- `mean`, `std`, and `ci95` (95% confidence interval) of $\lambda_2$ across the random trials
- `samples` — the raw array of per-trial $\lambda_2$ values
- If added to `main()` as shown in the file, also writes `output/lambda2_benchmark_samples.csv`

**How to read it:** compare the real network's `fiedler_val` to this benchmark's `mean`.
A ratio below 1 means the real network is less connected than a randomly-wired network of
the same size/degree would typically be.

---

