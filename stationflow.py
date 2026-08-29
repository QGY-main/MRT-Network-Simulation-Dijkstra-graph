"""
Input : resources/202504/node_simplified.csv (col: PT_CODE)
        resources/202504/origin_destination_train_202504.csv (cols: ORIGIN_PT_CODE, DESTINATION_PT_CODE, TOTAL_TRIPS, TIME_PER_HOUR)
        resources/stn_coor_010326.csv (cols: station_code, lat, lon)
Output: output/*.csv (analysis tables), output/*.png (plots)
Set STATIONFLOW_DATA_DIR to change the resources/ location.
"""
import os
import csv
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from scipy.sparse import csr_matrix, issparse
from scipy.sparse.csgraph import dijkstra, laplacian
from scipy.sparse.linalg import eigsh
from scipy.optimize import curve_fit

warnings.filterwarnings("ignore", category=RuntimeWarning)

DATA_DIR = Path(os.environ.get("STATIONFLOW_DATA_DIR", "resources"))
STN_DATA_PATH = DATA_DIR / "202504" / "node_simplified.csv"
OD_DATA_PATH = DATA_DIR / "202504" / "origin_destination_train_202504.csv"
STN_COOR_PATH = DATA_DIR / "stn_coor_010326.csv"

STATION_CODES = {"NE", "EW", "NS", "CC", "DT", "TE", "BP", "SW", "SE", "PW", "PE", "CE", "CG"}
MAX_STATIONS_PER_LINE = 50
CAPACITY = 300
FREQUENCY = 12

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

TARGET_STATIONS = ["NE12/CC13", "NS9/TE2", "EW2/DT32", "EW8/CC9", "EW24/NS1"]

DEFAULT_IRREGULAR_PAIRS = [
    ("CE1", "CC4"), ("NE17", "PE1"), ("NE17", "PE7"), ("NE17", "PW1"), ("NE17", "PW7"),
    ("NE16", "SW1"), ("NE16", "SW8"), ("NE16", "SE1"), ("NE16", "SE5"),
    ("BP13", "BP6"), ("EW4", "DT35"),
]


def check_symmetry(matrix, label="matrix"):
    # Input : 2D matrix (dense or sparse)
    # Output: bool, True if matrix == matrix.T
    dense = matrix.toarray() if issparse(matrix) else np.asarray(matrix)
    res = np.allclose(dense, dense.T)
    print(f"[{label}] symmetry check {'PASSED' if res else 'FAILED'}")
    return res


class Coordinate:
    __slots__ = ("lat", "lon")

    def __init__(self, lat, lon):
        self.lat = lat
        self.lon = lon


def haversine_distance(c1, c2):
    # Input : two Coordinate objects
    # Output: distance in km (float)
    R = 6378.137
    lat1, lon1, lat2, lon2 = map(np.radians, (c1.lat, c1.lon, c2.lat, c2.lon))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return R * 2 * np.arcsin(np.sqrt(a))


def build_station_index(stn_data_path):
    # Input : path to node_simplified.csv (col: PT_CODE)
    # Output: nodes (sorted list of station codes),
    #         node2index (dict station_code -> int index),
    #         index2node (dict int index -> station_code)
    with open(stn_data_path, newline="") as f:
        node_set = {row["PT_CODE"] for row in csv.DictReader(f)}
    nodes = sorted(node_set)
    node2index = {name: i for i, name in enumerate(nodes)}
    index2node = {i: name for i, name in enumerate(nodes)}
    return nodes, node2index, index2node


def _build_stop_code_lookup(node2index):
    # Input : node2index dict
    # Output: dict mapping every individual stop code (e.g. "EW3") to its
    #         full node name (e.g. "NS5/EW3")
    lookup = {}
    for name in node2index:
        for part in name.split("/"):
            lookup[part] = name
    return lookup


def build_unweighted_adjacency(nodes, node2index, irregular_pairs=DEFAULT_IRREGULAR_PAIRS):
    # Input : nodes, node2index, optional list of (code_a, code_b) irregular edges
    # Output: n x n unweighted symmetric adjacency matrix (numpy array)
    n = len(nodes)
    stop_lookup = _build_stop_code_lookup(node2index)
    adj = np.zeros((n, n), dtype=np.float64)

    for code in STATION_CODES:
        prev_node = None
        for i in range(1, MAX_STATIONS_PER_LINE + 1):
            node = stop_lookup.get(f"{code}{i}")
            if node is None:
                continue
            if prev_node is not None and prev_node != node:
                a, b = node2index[prev_node], node2index[node]
                adj[a, b] = adj[b, a] = 1
            prev_node = node

    for code_a, code_b in (irregular_pairs or []):
        a = node2index.get(stop_lookup.get(code_a, ""))
        b = node2index.get(stop_lookup.get(code_b, ""))
        if a is None or b is None:
            continue
        adj[a, b] = adj[b, a] = 1

    return adj


def build_station_coordinates(stn_coor_path, node2index):
    # Input : path to stn_coor csv (cols: station_code, lat, lon), node2index
    # Output: dict station_code -> Coordinate, for every resolvable node
    raw = {}
    with open(stn_coor_path, newline="") as f:
        for row in csv.DictReader(f):
            raw[row["station_code"]] = Coordinate(float(row["lat"]), float(row["lon"]))

    stn_code_to_coor = {}
    for full_name in node2index:
        if full_name in raw:
            stn_code_to_coor[full_name] = raw[full_name]
            continue
        for part in full_name.split("/"):
            if part in raw:
                stn_code_to_coor[full_name] = raw[part]
                break
    return stn_code_to_coor


def load_od_data(od_data_path):
    # Input : path to OD csv (cols: ORIGIN_PT_CODE, DESTINATION_PT_CODE, TOTAL_TRIPS)
    # Output: numpy array of [origin, destination, total_trips] rows, grouped/summed
    df = pd.read_csv(od_data_path, usecols=["ORIGIN_PT_CODE", "DESTINATION_PT_CODE", "TOTAL_TRIPS"])
    grouped = df.groupby(["ORIGIN_PT_CODE", "DESTINATION_PT_CODE"])["TOTAL_TRIPS"].sum().reset_index()
    return grouped.to_numpy()


def route_and_weight(base_unweighted_graph, od_data_arr, node2index, verbose=False):
    # Input : unweighted adjacency matrix, OD array (from load_od_data), node2index
    # Output: weighted adjacency matrix (trips routed over shortest paths)
    weighted = base_unweighted_graph.copy().astype(float)
    dist_matrix, pred_matrix = dijkstra(csgraph=base_unweighted_graph, directed=False,
                                         return_predecessors=True)

    total = len(od_data_arr)
    for count, (ori_code, des_code, trips) in enumerate(od_data_arr, start=1):
        u, v = node2index.get(ori_code), node2index.get(des_code)
        if u is None or v is None or u == v:
            continue
        if pred_matrix[u, v] == -9999:
            continue
        curr = v
        while curr != u:
            prev = pred_matrix[u, curr]
            if prev == -9999:
                break
            weighted[prev, curr] += trips
            weighted[curr, prev] += trips
            curr = prev
        if verbose and count % 5000 == 0:
            print(f"  routed {count}/{total} OD pairs")
    return weighted


def fiedler_pair(weighted_graph, k=3, use_sparse=True):
    # Input : weighted adjacency matrix (dense or sparse)
    # Output: (fiedler_value, fiedler_vector, first-k eigenvalues, first-k eigenvectors)
    n = weighted_graph.shape[0]
    sparse_graph = weighted_graph if issparse(weighted_graph) else csr_matrix(weighted_graph)
    lap = laplacian(sparse_graph, normed=True, symmetrized=True)

    k_eff = max(min(k, n - 2), 2) if n > 10 else n
    if use_sparse and n > 10:
        try:
            vals, vecs = eigsh(lap, k=k_eff, sigma=0, which="LM")
            order = np.argsort(vals)
            vals, vecs = vals[order], vecs[:, order]
        except Exception:
            vals, vecs = np.linalg.eigh(lap.toarray())
    else:
        vals, vecs = np.linalg.eigh(lap.toarray())

    idx = 0
    while idx < len(vals) and np.isclose(vals[idx], 0.0, atol=1e-8):
        idx += 1
    idx = min(idx, len(vals) - 1)
    return vals[idx], vecs[:, idx], vals, vecs


def utilization_factor(weighted_graph, capacity, frequency):
    # Input : weighted adjacency matrix, capacity (pax/train), frequency (trains/hour)
    # Output: per-station utilization factor (numpy array)
    dense = weighted_graph.toarray() if issparse(weighted_graph) else np.asarray(weighted_graph)
    return dense.sum(axis=1) / (capacity * frequency)


def disrupt_edges(base_unweighted, node2index, edges):
    # Input : unweighted adjacency matrix, node2index, list of (station_a, station_b) edges
    # Output: (disrupted adjacency matrix, list of successfully disrupted edge labels)
    g = base_unweighted.copy()
    applied = []
    for a_name, b_name in edges:
        a, b = node2index.get(a_name), node2index.get(b_name)
        if a is None or b is None or g[a, b] == 0:
            continue
        g[a, b] = g[b, a] = 0
        applied.append(f"{a_name}-{b_name}")
    return g, applied


def disrupt_stations(base_unweighted, node2index, stations):
    # Input : unweighted adjacency matrix, node2index, list of station names
    # Output: (disrupted adjacency matrix, list of successfully disrupted stations)
    g = base_unweighted.copy()
    applied = []
    for name in stations:
        idx = node2index.get(name)
        if idx is None:
            continue
        g[idx, :] = 0
        g[:, idx] = 0
        applied.append(name)
    return g, applied


def disrupt_line(base_unweighted, node2index, line_code):
    # Input : unweighted adjacency matrix, node2index, line code substring (e.g. "NS")
    # Output: (disrupted adjacency matrix, list of disrupted stations on that line)
    stations = [name for name in node2index if line_code in name]
    return disrupt_stations(base_unweighted, node2index, stations)


def disrupt_random_edges(base_unweighted, num_edges, rng):
    # Input : unweighted adjacency matrix, number of edges to remove, numpy Generator
    # Output: (disrupted adjacency matrix, list of (i, j) index pairs removed)
    g = base_unweighted.copy()
    edge_idx = np.argwhere(np.triu(base_unweighted) == 1)
    chosen = rng.choice(len(edge_idx), size=num_edges, replace=False)
    applied = []
    for i in chosen:
        a, b = edge_idx[i]
        g[a, b] = g[b, a] = 0
        applied.append((int(a), int(b)))
    return g, applied


def analyze_disruption(disrupted_unweighted, od_data_arr, node2index, index2node,
                        baseline_fiedler_val, baseline_fiedler_vec, baseline_util,
                        capacity=CAPACITY, frequency=FREQUENCY):
    # Input : disrupted adjacency matrix, OD array, node maps, baseline fiedler value/vector/utilization
    # Output: dict with weighted_graph, fiedler_value, fiedler_vector, utilization,
    #         avg_abs_change_eigenvector, delta_utilization, avg_change_utilization
    weighted = route_and_weight(disrupted_unweighted, od_data_arr, node2index)
    fval, fvec, _, _ = fiedler_pair(weighted)
    util = utilization_factor(weighted, capacity, frequency)

    delta_evec = np.abs(fvec) - np.abs(baseline_fiedler_vec)
    delta_util = util - baseline_util

    return {
        "weighted_graph": weighted,
        "fiedler_value": fval,
        "fiedler_vector": fvec,
        "utilization": util,
        "avg_abs_change_eigenvector": float(np.mean(np.abs(delta_evec))),
        "delta_utilization": delta_util,
        "avg_change_utilization": float(np.mean(delta_util[delta_util != 0])) if np.any(delta_util != 0) else 0.0,
    }


def save_disruption_result(label, applied, result, filename, nodes, index2node, fiedler_val):
    # Input : label, disrupted item names, analyze_disruption() result, output filename
    # Output: writes output/<filename> CSV, returns None
    df_out = pd.DataFrame({
        "Station/Edge Disrupted": [", ".join(map(str, applied))] * len(nodes),
        "Station": [index2node[i] for i in range(len(nodes))],
        "Pre-Disruption Fiedler Value": [fiedler_val] * len(nodes),
        "Post-Disruption Fiedler Value": [result["fiedler_value"]] * len(nodes),
        "Avg |Eigenvector| Change": [result["avg_abs_change_eigenvector"]] * len(nodes),
        "Utilization Factor Change": result["delta_utilization"],
    })
    path = OUTPUT_DIR / filename
    df_out.to_csv(path, index=False)
    print(f"[{label}] Fiedler value {fiedler_val:.4f} -> {result['fiedler_value']:.4f} (saved to {path})")


def reconstruct_path(pred_matrix, start_idx, end_idx):
    # Input : predecessor matrix (from dijkstra), start/end station index
    # Output: list of station indices along the shortest path, or None if unreachable
    if start_idx == end_idx:
        return [start_idx]
    if pred_matrix[start_idx, end_idx] == -9999:
        return None
    path, current = [end_idx], end_idx
    while current != start_idx:
        current = pred_matrix[start_idx, current]
        if current == -9999:
            return None
        path.append(current)
    path.reverse()
    return path


def build_hourly_station_throughput(df_hourly_od, node2index, index2node, pred_matrix):
    # Input : hourly-grouped OD dataframe (cols: hour, ORIGIN_PT_CODE, DESTINATION_PT_CODE, TOTAL_TRIPS),
    #         node2index, index2node, predecessor matrix
    # Output: dataframe (cols: hour, station, TOTAL_THROUGHPUT)
    records = []
    for hour in sorted(df_hourly_od["hour"].unique()):
        df_hour = df_hourly_od[df_hourly_od["hour"] == hour]
        totals = {s: 0 for s in index2node.values()}
        for _, row in df_hour.iterrows():
            o, d, trips = row["ORIGIN_PT_CODE"], row["DESTINATION_PT_CODE"], row["TOTAL_TRIPS"]
            if o not in node2index or d not in node2index:
                continue
            path = reconstruct_path(pred_matrix, node2index[o], node2index[d])
            if path is None:
                continue
            for idx in path:
                totals[index2node[idx]] += trips
        for station, total in totals.items():
            records.append({"hour": hour, "station": station, "TOTAL_THROUGHPUT": total})
    return pd.DataFrame(records)


def two_peak_logistic(t, B, K1, gamma1, t1_start, t1_end, K2, gamma2, t2_start, t2_end):
    # Input : time array t, curve parameters
    # Output: modeled throughput array (AM peak + PM peak sigmoid bumps + baseline)
    def sigmoid(x):
        return 1 / (1 + np.exp(-x))
    morning = K1 * np.minimum(sigmoid(gamma1 * (t - t1_start)), sigmoid(gamma1 * (t1_end - t)))
    evening = K2 * np.minimum(sigmoid(gamma2 * (t - t2_start)), sigmoid(gamma2 * (t2_end - t)))
    return B + morning + evening


def candidate_edges_by_distance(nodes, node2index, index2node, unweighted_adj_matrix, stn_code_to_coor):
    # Input : nodes, node2index, index2node, unweighted adjacency matrix, station coord dict
    # Output: list of (i, j) index pairs for unconnected stations closer than the median edge length
    n = len(nodes)
    coords = [stn_code_to_coor.get(index2node[i]) for i in range(n)]
    have_coord = np.array([c is not None for c in coords])
    lat = np.array([c.lat if c else 0.0 for c in coords])
    lon = np.array([c.lon if c else 0.0 for c in coords])

    R = 6378.137
    lat_r, lon_r = np.radians(lat), np.radians(lon)
    dlat = lat_r[:, None] - lat_r[None, :]
    dlon = lon_r[:, None] - lon_r[None, :]
    a = np.sin(dlat / 2) ** 2 + np.cos(lat_r[:, None]) * np.cos(lat_r[None, :]) * np.sin(dlon / 2) ** 2
    dist_matrix = R * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))

    iu = np.triu_indices(n, k=1)
    valid = have_coord[iu[0]] & have_coord[iu[1]]
    existing_mask = unweighted_adj_matrix[iu] == 1
    existing_lengths = dist_matrix[iu][existing_mask & valid]
    median_length = np.median(existing_lengths) if len(existing_lengths) else np.inf

    candidate_mask = (~existing_mask) & valid & (dist_matrix[iu] <= median_length)
    return list(zip(iu[0][candidate_mask], iu[1][candidate_mask]))


def analyze_network_expansion(candidates, unweighted_adj_matrix, od_data_arr, node2index, index2node,
                               baseline_fiedler_val, baseline_fiedler_vec, baseline_util,
                               capacity=CAPACITY, frequency=FREQUENCY):
    # Input : candidate (i, j) edge list, unweighted adjacency matrix, OD array, node maps, baseline stats
    # Output: dataframe scoring each candidate edge (fiedler value, eigenvector/utilization change)
    results = []
    for k, (i, j) in enumerate(candidates, start=1):
        trial_graph = unweighted_adj_matrix.copy()
        trial_graph[i, j] = trial_graph[j, i] = 1

        weighted = route_and_weight(trial_graph, od_data_arr, node2index)
        fval, fvec, _, _ = fiedler_pair(weighted)
        if np.dot(fvec, baseline_fiedler_vec) < 0:
            fvec = -fvec
        util = utilization_factor(weighted, capacity, frequency)

        avg_change_evec = float(np.mean(np.abs(fvec - baseline_fiedler_vec)))
        delta_util = util - baseline_util
        decreased = delta_util[delta_util < 0]
        avg_change_util_dec = float(decreased.mean()) if len(decreased) else 0.0

        results.append({
            "EDGE_BETWEEN_STN": f"{index2node[i]} - {index2node[j]}",
            "original_eigen_value": baseline_fiedler_val,
            "current_eigen_value": fval,
            "avg_change_eigen_vector": avg_change_evec,
            "avg_change_util_factor_decreased": avg_change_util_dec,
        })
    return pd.DataFrame(results)


def rank_by_composite_score(df, w_eigval=1 / 3, w_eigvec=1 / 3, w_util=1 / 3, top_k=10):
    # Input : network expansion dataframe, weights, top_k
    # Output: top_k rows sorted by composite score (higher = better)
    df = df.copy()
    df["score_eigval"] = df["current_eigen_value"].rank(ascending=False, method="average")
    df["score_eigval"] = 1 - (df["score_eigval"] - 1) / max(len(df) - 1, 1)
    df["score_eigvec"] = df["avg_change_eigen_vector"].rank(ascending=True, method="average")
    df["score_eigvec"] = 1 - (df["score_eigvec"] - 1) / max(len(df) - 1, 1)
    df["score_util"] = df["avg_change_util_factor_decreased"].rank(ascending=True, method="average")
    df["score_util"] = 1 - (df["score_util"] - 1) / max(len(df) - 1, 1)
    df["composite_score"] = w_eigval * df["score_eigval"] + w_eigvec * df["score_eigvec"] + w_util * df["score_util"]
    return df.sort_values("composite_score", ascending=False).head(top_k)


def plot_network(unweighted_adj_matrix, node2index, index2node, stn_code_to_coor,
                  highlight_stations=None, highlight_edges=None, title="MRT Network Graph",
                  savepath=None):
    # Input : unweighted adjacency matrix, node maps, station coords, optional highlights, optional savepath
    # Output: shows plot; if savepath given, also writes PNG to that path
    highlight_stations = set(highlight_stations or [])
    plt.figure(figsize=(16, 12))

    for stn_code, idx in node2index.items():
        coord = stn_code_to_coor.get(stn_code)
        if coord is None:
            continue
        if stn_code in highlight_stations:
            plt.plot(coord.lon, coord.lat, 'o', color='orangered', markersize=14,
                      markeredgecolor='black', markeredgewidth=2, zorder=5)
            plt.text(coord.lon, coord.lat, stn_code, fontsize=11, ha='right', va='bottom',
                      color='darkblue', weight='bold', zorder=6)
        else:
            plt.plot(coord.lon, coord.lat, 'o', color='steelblue', markersize=6, zorder=1)

    n = len(index2node)
    for i in range(n):
        for j in range(i + 1, n):
            if unweighted_adj_matrix[i, j] != 1:
                continue
            stn_i, stn_j = index2node[i], index2node[j]
            c_i, c_j = stn_code_to_coor.get(stn_i), stn_code_to_coor.get(stn_j)
            if c_i is None or c_j is None:
                continue
            plt.plot([c_i.lon, c_j.lon], [c_i.lat, c_j.lat], 'k-', linewidth=1.5, alpha=0.4, zorder=0)

    for a, b in (highlight_edges or []):
        c_a, c_b = stn_code_to_coor.get(a), stn_code_to_coor.get(b)
        if c_a and c_b:
            plt.plot([c_a.lon, c_b.lon], [c_a.lat, c_b.lat], color='darkred', linewidth=3, zorder=3)

    plt.xlabel('Longitude')
    plt.ylabel('Latitude')
    plt.title(title)
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    if savepath:
        plt.savefig(savepath, dpi=150)
    plt.show()
    plt.close()

import numpy as np
import networkx as nx
 
 # Computing benchmark for eigenvalue
def compute_lambda2_benchmark(unweighted_adj_matrix, od_data_arr, node2index,
                               n_trials=500, seed=7, verbose=True):
    # Input : unweighted_adj_matrix (real topology), od_data_arr, node2index,
    #         n_trials (number of random-topology samples), seed
    # Output: dict with 'mean', 'std', 'ci95' (2.5/97.5 percentiles), 'samples' (array)
    n = unweighted_adj_matrix.shape[0]
    G_real = nx.from_numpy_array(unweighted_adj_matrix)
    deg_seq = [d for _, d in G_real.degree()]
 
    rng = np.random.default_rng(seed)
    samples = []
    trial, attempts = 0, 0
    max_attempts = n_trials * 20
 
    while trial < n_trials and attempts < max_attempts:
        attempts += 1
        H = G_real.copy()
        try:
            # preserves exact degree sequence, keeps graph connected
            nx.connected_double_edge_swap(H, nswap=4 * H.number_of_edges(),
                                           seed=int(rng.integers(1e9)))
        except nx.NetworkXError:
            continue  # occasionally fails on sparse/awkward degree sequences; skip and retry
        if not nx.is_connected(H):
            continue
 
        rand_adj = nx.to_numpy_array(H)
        rand_weighted = route_and_weight(rand_adj, od_data_arr, node2index)
        fval, _, _, _ = fiedler_pair(rand_weighted)
        samples.append(fval)
        trial += 1
 
        if verbose and trial % 50 == 0:
            print(f"  benchmark trial {trial}/{n_trials}")
 
    samples = np.array(samples)
    result = {
        "mean": float(samples.mean()),
        "std": float(samples.std()),
        "ci95": tuple(np.percentile(samples, [2.5, 97.5])),
        "samples": samples,
        "n_valid_trials": len(samples),
    }
    return result

def check_constraints(c, f, cost_per_new_edge, num_new_edge, op_cost_per_capacity, freq_min, freq_max):
    # Input : capacity array, frequency array, cost constants, freq bounds
    # Output: boolean array, True where (c, f) satisfies all constraints
    restriction_a = cost_per_new_edge * num_new_edge <= 1_487_559_500
    restriction_b = op_cost_per_capacity * c * f * 24 * 365 <= 301_440_500
    restriction_c = f * c <= 301_440_500 / op_cost_per_capacity / 24 / 365
    restriction_d = (f >= freq_min) & (f <= freq_max)
    return restriction_a & restriction_b & restriction_c & restriction_d


def get_pareto_frontier(costs, satisfactions):
    # Input : normalized cost array, normalized satisfaction array (both to be minimized)
    # Output: boolean mask, True for points on the Pareto frontier
    n = len(costs)
    mask = np.ones(n, dtype=bool)
    for i in range(n):
        if not mask[i]:
            continue
        dominators = (costs <= costs[i]) & (satisfactions <= satisfactions[i])
        dominators[i] = False
        if dominators.any():
            mask[i] = False
    return mask


def normalize(x):
    # Input : numpy array
    # Output: array rescaled to [0, 1] (zeros if constant input)
    rng_ = x.max() - x.min()
    return np.zeros_like(x) if rng_ == 0 else (x - x.min()) / rng_


def main():
    for p in (STN_DATA_PATH, OD_DATA_PATH, STN_COOR_PATH):
        if not p.exists():
            print(f"Missing data file: {p}. Set STATIONFLOW_DATA_DIR or place data under '{DATA_DIR}/'.")

    # --- graph construction ---
    # Input : STN_DATA_PATH, STN_COOR_PATH
    # Output: nodes, node2index, index2node, unweighted_adj_matrix, stn_code_to_coor
    nodes, node2index, index2node = build_station_index(STN_DATA_PATH)
    unweighted_adj_matrix = build_unweighted_adjacency(nodes, node2index)
    check_symmetry(unweighted_adj_matrix, "unweighted_adj_matrix")
    stn_code_to_coor = build_station_coordinates(STN_COOR_PATH, node2index)

    # --- weighted graph from OD flows ---
    # Input : OD_DATA_PATH, unweighted_adj_matrix
    # Output: weighted_adj_matrix
    od_data_arr = load_od_data(OD_DATA_PATH)
    weighted_adj_matrix = route_and_weight(unweighted_adj_matrix, od_data_arr, node2index, verbose=True)
    check_symmetry(weighted_adj_matrix, "weighted_adj_matrix")

    # --- spectral (Fiedler) analysis ---
    # Input : weighted_adj_matrix
    # Output: fiedler_val, fiedler_vec, eigen_vals, eigen_vecs
    fiedler_val, fiedler_vec, eigen_vals, eigen_vecs = fiedler_pair(weighted_adj_matrix)
    print(f"Fiedler value: {fiedler_val:.6f}")

    # --- vulnerability plot ---
    # Input : fiedler_vec, index2node
    # Output: output/vulnerable_stations.png
    sec_eigen_vec_abs = np.abs(fiedler_vec)
    top5_idx = np.argpartition(sec_eigen_vec_abs, -min(5, len(sec_eigen_vec_abs)))[-5:]
    top5_names = [index2node[i] for i in top5_idx]
    plt.figure(figsize=(10, 6))
    plt.bar(top5_names, sec_eigen_vec_abs[top5_idx])
    plt.title("Fiedler Eigenvector - Five Most Vulnerable Stations")
    plt.xlabel("Station")
    plt.ylabel("|Eigenvector value|")
    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / "vulnerable_stations.png", dpi=150)
    plt.show()
    plt.close()

    # --- utilization factor ---
    # Input : weighted_adj_matrix, CAPACITY, FREQUENCY
    # Output: baseline_util (per-station array)
    baseline_util = utilization_factor(weighted_adj_matrix, CAPACITY, FREQUENCY)

    # --- selected-station eigenvector export ---
    # Input : TARGET_STATIONS, fiedler_vec, node2index
    # Output: output/selected_stations_eigenvector_values.csv
    rows = [{"stn_name": name, "abs_eigen_vector": abs(fiedler_vec[node2index[name]])}
            for name in TARGET_STATIONS if name in node2index]
    pd.DataFrame(rows).to_csv(OUTPUT_DIR / "selected_stations_eigenvector_values.csv", index=False)

    # --- hourly logistic regression of station throughput ---
    # Input : OD_DATA_PATH, unweighted_adj_matrix, TARGET_STATIONS
    # Output: output/station_throughput_logistic.png
    np.random.seed(42)
    df_od_full = pd.read_csv(OD_DATA_PATH, usecols=["ORIGIN_PT_CODE", "DESTINATION_PT_CODE", "TOTAL_TRIPS", "TIME_PER_HOUR"])
    df_od_full = df_od_full.rename(columns={"TIME_PER_HOUR": "hour"})
    df_od_hourly = df_od_full.groupby(["hour", "ORIGIN_PT_CODE", "DESTINATION_PT_CODE"])["TOTAL_TRIPS"].sum().reset_index()
    df_od_hourly["hour"] = df_od_hourly["hour"].astype(float)
    _, pred_matrix_baseline = dijkstra(csgraph=unweighted_adj_matrix, directed=False, return_predecessors=True)
    df_station_hourly = build_hourly_station_throughput(df_od_hourly, node2index, index2node, pred_matrix_baseline)

    plt.figure(figsize=(12, 6))
    colors = ["#1f77b4", "#ebbda7", "#5B8E7D", "#9b9ab3", "#6D6875"]
    for i, station in enumerate(TARGET_STATIONS):
        data = df_station_hourly[df_station_hourly["station"] == station].sort_values("hour").reset_index(drop=True)
        if data.empty or data["TOTAL_THROUGHPUT"].max() == 0:
            continue
        t_data, y_data = data["hour"].values, data["TOTAL_THROUGHPUT"].values
        p0 = [float(y_data.min()), float(y_data.max() / 2), 1, 6, 10, float(y_data.max() / 2), 1, 17, 20]
        try:
            popt, _ = curve_fit(two_peak_logistic, t_data, y_data, p0=p0, maxfev=20000)
        except RuntimeError:
            popt = p0
        t_fit = np.linspace(0, 23, 400)
        y_fit = two_peak_logistic(t_fit, *popt)
        plt.plot(t_fit, y_fit, color=colors[i], linewidth=2.2, label=station)
        plt.scatter(t_data, y_data, color=colors[i], s=35, alpha=0.6, edgecolor="black", linewidth=0.5, zorder=5)
    plt.axvspan(6, 9, color="red", alpha=0.1, label="Morning peak")
    plt.axvspan(16, 19, color="green", alpha=0.1, label="Evening peak")
    plt.title("Time-Based Logistic Regression of Station Throughput")
    plt.xlabel("Time of Day")
    plt.ylabel("Hourly Station Throughput")
    plt.legend(title="Station", fontsize=9)
    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / "station_throughput_logistic.png", dpi=150)
    plt.show()
    plt.close()

    # --- disruption scenarios ---
    # Input : edit EDGES_TO_DISRUPT / STATIONS_TO_DISRUPT / LINE_TO_DISRUPT / NUM_RANDOM_EDGES below
    # Output: output/edge_disruption_analysis.csv, output/station_disruption_analysis.csv,
    #         output/<line>_line_disruption_analysis.csv, output/random_disruption_analysis.csv
    EDGES_TO_DISRUPT = [("NS1", "NS2")]
    disrupted_graph, applied_edges = disrupt_edges(unweighted_adj_matrix, node2index, EDGES_TO_DISRUPT)
    if applied_edges:
        result = analyze_disruption(disrupted_graph, od_data_arr, node2index, index2node,
                                     fiedler_val, fiedler_vec, baseline_util)
        save_disruption_result("Edge disruption", applied_edges, result, "edge_disruption_analysis.csv",
                                nodes, index2node, fiedler_val)

    STATIONS_TO_DISRUPT = [nodes[0]]
    disrupted_graph, applied_stations = disrupt_stations(unweighted_adj_matrix, node2index, STATIONS_TO_DISRUPT)
    if applied_stations:
        result = analyze_disruption(disrupted_graph, od_data_arr, node2index, index2node,
                                     fiedler_val, fiedler_vec, baseline_util)
        save_disruption_result("Station disruption", applied_stations, result, "station_disruption_analysis.csv",
                                nodes, index2node, fiedler_val)

    LINE_TO_DISRUPT = "NS"
    disrupted_graph, applied_stations = disrupt_line(unweighted_adj_matrix, node2index, LINE_TO_DISRUPT)
    if applied_stations:
        result = analyze_disruption(disrupted_graph, od_data_arr, node2index, index2node,
                                     fiedler_val, fiedler_vec, baseline_util)
        save_disruption_result(f"Line '{LINE_TO_DISRUPT}' disruption", applied_stations, result,
                                f"{LINE_TO_DISRUPT}_line_disruption_analysis.csv", nodes, index2node, fiedler_val)

    NUM_RANDOM_EDGES = 2
    rng = np.random.default_rng(42)
    disrupted_graph, applied_edges_idx = disrupt_random_edges(unweighted_adj_matrix, NUM_RANDOM_EDGES, rng)
    applied_names = [f"{index2node[a]}-{index2node[b]}" for a, b in applied_edges_idx]
    result = analyze_disruption(disrupted_graph, od_data_arr, node2index, index2node,
                                 fiedler_val, fiedler_vec, baseline_util)
    save_disruption_result("Random disruption", applied_names, result, "random_disruption_analysis.csv",
                            nodes, index2node, fiedler_val)

    # --- weighted Monte Carlo disruption ---
    # Input : NUM_TRIALS, MAX_EDGES_PER_TRIAL below, unweighted_adj_matrix, od_data_arr
    # Output: output/monte_carlo_disruption_summary.csv
    NUM_TRIALS = 500
    MAX_EDGES_PER_TRIAL = 3
    rng = np.random.default_rng(7)
    station_weights = np.abs(fiedler_vec)
    station_weights = station_weights / station_weights.sum() if station_weights.sum() > 0 else np.full(len(nodes), 1 / len(nodes))
    edge_list = np.argwhere(np.triu(unweighted_adj_matrix) == 1)

    trial_fiedler_vals = np.empty(NUM_TRIALS)
    trial_util = np.empty((NUM_TRIALS, len(nodes)))
    trial_disrupted_stations = []

    for t in range(NUM_TRIALS):
        n_edges = rng.integers(1, MAX_EDGES_PER_TRIAL + 1)
        endpoint_weight = station_weights[edge_list[:, 0]] + station_weights[edge_list[:, 1]]
        edge_probs = endpoint_weight / endpoint_weight.sum()
        chosen = rng.choice(len(edge_list), size=min(n_edges, len(edge_list)), replace=False, p=edge_probs)

        g = unweighted_adj_matrix.copy()
        disrupted_stns = set()
        for i in chosen:
            a, b = edge_list[i]
            g[a, b] = g[b, a] = 0
            disrupted_stns.add(int(a))
            disrupted_stns.add(int(b))
        trial_disrupted_stations.append(disrupted_stns)

        weighted = route_and_weight(g, od_data_arr, node2index)
        fval, _, _, _ = fiedler_pair(weighted)
        trial_fiedler_vals[t] = fval
        trial_util[t] = utilization_factor(weighted, CAPACITY, FREQUENCY)

    n = len(nodes)
    eig_sum, eig_cnt = np.zeros(n), np.zeros(n)
    util_sum, util_cnt = np.zeros(n), np.zeros(n)
    for t, stns in enumerate(trial_disrupted_stations):
        for s in stns:
            eig_sum[s] += trial_fiedler_vals[t]
            eig_cnt[s] += 1
            util_sum[s] += trial_util[t, s] - baseline_util[s]
            util_cnt[s] += 1

    mc_rows = []
    for i in range(n):
        if eig_cnt[i] == 0:
            continue
        mc_rows.append({
            "Station": index2node[i],
            "Pre-Disruption Fiedler Value": fiedler_val,
            "Avg Post-Disruption Fiedler Value": eig_sum[i] / eig_cnt[i],
            "Avg Own Utilization Change": util_sum[i] / util_cnt[i],
            "Times Disrupted": int(eig_cnt[i]),
        })
    pd.DataFrame(mc_rows).sort_values("Avg Post-Disruption Fiedler Value").to_csv(
        OUTPUT_DIR / "monte_carlo_disruption_summary.csv", index=False)

    # --- network expansion analysis ---
    # Input : MAX_CANDIDATES below, unweighted_adj_matrix, stn_code_to_coor, od_data_arr
    # Output: output/network_expansion_analysis.csv
    MAX_CANDIDATES = 200
    candidates = candidate_edges_by_distance(nodes, node2index, index2node, unweighted_adj_matrix, stn_code_to_coor)
    if MAX_CANDIDATES is not None and len(candidates) > MAX_CANDIDATES:
        rng = np.random.default_rng(0)
        sel = rng.choice(len(candidates), size=MAX_CANDIDATES, replace=False)
        candidates = [candidates[i] for i in sel]

    df_expansion = analyze_network_expansion(candidates, unweighted_adj_matrix, od_data_arr, node2index, index2node,
                                              fiedler_val, fiedler_vec, baseline_util)
    df_expansion.to_csv(OUTPUT_DIR / "network_expansion_analysis.csv", index=False)

    # --- rank expansion candidates ---
    # Input : df_expansion
    # Output: printed top-10 table
    if len(df_expansion):
        top_candidates = rank_by_composite_score(df_expansion)
        cols = ["EDGE_BETWEEN_STN", "current_eigen_value", "avg_change_eigen_vector",
                "avg_change_util_factor_decreased", "composite_score"]
        print(top_candidates[cols].to_string(index=False))

    # --- diagrams ---
    # Input : unweighted_adj_matrix, node2index, index2node, stn_code_to_coor, df_expansion
    # Output: output/network_original.png, output/network_top_candidate.png
    plot_network(unweighted_adj_matrix, node2index, index2node, stn_code_to_coor,
                 title="Original MRT Network Graph", savepath=OUTPUT_DIR / "network_original.png")

    if len(df_expansion):
        best_edge = rank_by_composite_score(df_expansion, top_k=1).iloc[0]["EDGE_BETWEEN_STN"]
        a_name, b_name = [s.strip() for s in best_edge.split(" - ")]
        plot_network(unweighted_adj_matrix, node2index, index2node, stn_code_to_coor,
                     highlight_stations=[a_name, b_name], highlight_edges=[(a_name, b_name)],
                     title=f"Improved Network - Highlighted Candidate Edge: {best_edge}",
                     savepath=OUTPUT_DIR / "network_top_candidate.png")

    # --- Pareto frontier: capacity vs frequency ---
    # Input : weighted_adj_matrix, cost/constraint constants below
    # Output: output/pareto_frontier.png, printed optimal (capacity, frequency)
    NUM_SAMPLES = 100_000
    COST_PER_NEW_EDGE = 520_000_000
    NUM_NEW_EDGE = 2
    OPERATIONAL_COST_PER_CAPACITY = 2.15
    BETA, ALPHA = 0.7, 1
    FREQ_MIN, FREQ_MAX = 1, 50

    rng = np.random.default_rng(0)
    raw_caps = rng.choice((320 * 6, 310 * 3), NUM_SAMPLES)
    raw_freqs = rng.uniform(FREQ_MIN, FREQ_MAX, NUM_SAMPLES)
    valid = check_constraints(raw_caps, raw_freqs, COST_PER_NEW_EDGE, NUM_NEW_EDGE,
                               OPERATIONAL_COST_PER_CAPACITY, FREQ_MIN, FREQ_MAX)
    caps, freqs = raw_caps[valid], raw_freqs[valid]

    flow_per_node = weighted_adj_matrix.sum(axis=1)
    raw_cost = COST_PER_NEW_EDGE * NUM_NEW_EDGE + OPERATIONAL_COST_PER_CAPACITY * (BETA * caps + freqs * ALPHA)
    raw_sat = flow_per_node.sum() / (caps * freqs * len(flow_per_node))

    cost_norm, sat_norm = normalize(raw_cost), normalize(raw_sat)
    pareto_mask = get_pareto_frontier(cost_norm, sat_norm)
    p_cost, p_sat = cost_norm[pareto_mask], sat_norm[pareto_mask]

    diff = np.sqrt(p_sat ** 2 + p_cost ** 2)
    best_local = np.argmin(diff)
    best_global = np.where(pareto_mask)[0][best_local]

    plt.figure(figsize=(10, 6))
    plt.scatter(sat_norm, cost_norm, c='gray', s=5, alpha=0.5, label='Valid Solutions')
    order = np.argsort(p_sat)
    plt.plot(p_sat[order], p_cost[order], c='red', linewidth=2, label='Pareto Frontier')
    plt.scatter(cost_norm[best_global], sat_norm[best_global], c='green', marker='*', s=300,
                edgecolors='black', zorder=10, label='Balanced Solution')
    plt.title(f'Pareto Optimization: Satisfaction vs Cost (N={len(caps)})')
    plt.xlabel('Satisfaction Factor (Normalized, smaller is better)')
    plt.ylabel('Cost Factor (Normalized, smaller is better)')
    plt.grid(True, linestyle='--', alpha=0.7)
    plt.legend()
    plt.tight_layout()
    plt.savefig(OUTPUT_DIR / "pareto_frontier.png", dpi=150)
    plt.show()
    plt.close()

    print(f"Capacity  : {caps[best_global]:.0f} pax/train")
    print(f"Frequency : {freqs[best_global]:.2f} trains/hour")
    print(f"Total Flow: {caps[best_global] * freqs[best_global]:.0f} pax/hour")


if __name__ == "__main__":
    main()
