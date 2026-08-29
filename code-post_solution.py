"""
Post-solution ("improved network")

What this does:
  1. Builds one improved graph by adding the top-2 candidate edges from the
     network expansion analysis.
  2. Reruns route_and_weight + fiedler_pair on that combined graph to get the
     "after" network-wide Fiedler value (lambda2).
  3. Reruns the 5-station disruption scenario (TARGET_STATIONS) on both the
     original and improved graphs, for comparison.

Output: output/post_solution_network_summary.csv
        output/post_solution_disruption_comparison.csv
"""
import numpy as np
import pandas as pd

from stationflow import (
    OUTPUT_DIR, CAPACITY, FREQUENCY, TARGET_STATIONS,
    compute_baseline,
    route_and_weight, fiedler_pair, utilization_factor,
    disrupt_stations, analyze_disruption, rank_by_composite_score,
)


def build_combined_improved_graph(unweighted_adj_matrix, node2index, df_expansion, top_k=2):
    # Input : original unweighted adjacency matrix, node2index, network_expansion
    #         results dataframe, number of top candidate edges to add together
    # Output: (combined adjacency matrix with top_k edges added, list of
    #         (station_a, station_b) name pairs that were added)
    top = rank_by_composite_score(df_expansion, top_k=top_k)
    combined = unweighted_adj_matrix.copy()
    added_edges = []
    for edge_str in top["EDGE_BETWEEN_STN"]:
        a_name, b_name = [s.strip() for s in edge_str.split(" - ")]
        i, j = node2index[a_name], node2index[b_name]
        combined[i, j] = combined[j, i] = 1
        added_edges.append((a_name, b_name))
    return combined, added_edges


def compute_post_solution_results(unweighted_adj_matrix, combined_graph, od_data_arr,
                                   node2index, index2node, fiedler_val, fiedler_vec,
                                   baseline_util, target_stations=TARGET_STATIONS,
                                   capacity=CAPACITY, frequency=FREQUENCY):

    weighted_improved = route_and_weight(combined_graph, od_data_arr, node2index)
    fval_improved, fvec_improved, _, _ = fiedler_pair(weighted_improved)
    if np.dot(fvec_improved, fiedler_vec) < 0:
        fvec_improved = -fvec_improved
    util_improved = utilization_factor(weighted_improved, capacity, frequency)

    network_summary = {
        "Fiedler value (before)": fiedler_val,
        "Fiedler value (after)": fval_improved,
        "Fiedler value change %": (fval_improved - fiedler_val) / fiedler_val * 100 if fiedler_val else np.nan,
        "Mean utilization (before)": float(baseline_util.mean()),
        "Mean utilization (after)": float(util_improved.mean()),
        "Mean utilization change %": (util_improved.mean() - baseline_util.mean())
                                       / baseline_util.mean() * 100 if baseline_util.mean() else np.nan,
    }

    # --- 5-station disruption scenario, rerun on original vs improved graph ---
    rows = []
    for stn in target_stations:
        if stn not in node2index:
            continue

        d_before_graph, _ = disrupt_stations(unweighted_adj_matrix, node2index, [stn])
        res_before = analyze_disruption(d_before_graph, od_data_arr, node2index, index2node,
                                         fiedler_val, fiedler_vec, baseline_util, capacity, frequency)

        d_after_graph, _ = disrupt_stations(combined_graph, node2index, [stn])
        res_after = analyze_disruption(d_after_graph, od_data_arr, node2index, index2node,
                                        fval_improved, fvec_improved, util_improved, capacity, frequency)

        retained_before = (res_before["fiedler_value"] / fiedler_val * 100) if fiedler_val else np.nan
        retained_after = (res_after["fiedler_value"] / fval_improved * 100) if fval_improved else np.nan

        crowding_before = res_before["avg_change_utilization"]
        crowding_reduction_pct = (
            (crowding_before - res_after["avg_change_utilization"]) / abs(crowding_before) * 100
            if crowding_before not in (0, None) else np.nan
        )

        rows.append({
            "Station": stn,
            "Fiedler pre-disruption (before improvement)": fiedler_val,
            "Fiedler post-disruption (before improvement)": res_before["fiedler_value"],
            "Connectivity retained % (before improvement)": retained_before,
            "Fiedler pre-disruption (after improvement)": fval_improved,
            "Fiedler post-disruption (after improvement)": res_after["fiedler_value"],
            "Connectivity retained % (after improvement)": retained_after,
            "Avg crowding change (before improvement)": crowding_before,
            "Avg crowding change (after improvement)": res_after["avg_change_utilization"],
            "Crowding change reduction %": crowding_reduction_pct,
        })

    df_comparison = pd.DataFrame(rows)
    return network_summary, df_comparison, weighted_improved, fval_improved, fvec_improved, util_improved


def main():
    b = compute_baseline()
    unweighted_adj_matrix = b["unweighted_adj_matrix"]
    node2index, index2node = b["node2index"], b["index2node"]
    od_data_arr = b["od_data_arr"]
    fiedler_val, fiedler_vec = b["fiedler_val"], b["fiedler_vec"]
    baseline_util = b["baseline_util"]
    df_expansion = b["df_expansion"]

    if not len(df_expansion):
        print("No expansion candidates available; cannot build an improved network. Exiting.")
        return

    combined_graph, added_edges = build_combined_improved_graph(
        unweighted_adj_matrix, node2index, df_expansion, top_k=2)
    print(f"Added edges: {added_edges}")

    network_summary, df_comparison, weighted_improved, fval_improved, fvec_improved, util_improved = \
        compute_post_solution_results(
            unweighted_adj_matrix, combined_graph, od_data_arr,
            node2index, index2node, fiedler_val, fiedler_vec, baseline_util)

    pd.DataFrame([network_summary]).to_csv(OUTPUT_DIR / "post_solution_network_summary.csv", index=False)
    df_comparison.to_csv(OUTPUT_DIR / "post_solution_disruption_comparison.csv", index=False)
    print(df_comparison.to_string(index=False))


if __name__ == "__main__":
    main()
