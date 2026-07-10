import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import adjusted_rand_score, normalized_mutual_info_score
from sklearn.decomposition import PCA
from sklearn.manifold import TSNE
import matplotlib.pyplot as plt
import matplotlib.cm as cm


# -----------------------------------------------------------------------
# STEP 1: Prepare data
# -----------------------------------------------------------------------
def prepare_clustering_data(clauses):
    """
    clauses: list of dicts like {"clause_ref": ..., "rule_id": ..., "embedding": [...]}
    Returns embeddings matrix, true labels (rule_id), and clause_refs.
    """
    embeddings = np.array([c["embedding"] for c in clauses])
    rule_ids = [c["rule_id"] for c in clauses]
    clause_refs = [c["clause_ref"] for c in clauses]
    return embeddings, rule_ids, clause_refs


# -----------------------------------------------------------------------
# STEP 2: Run clustering
# -----------------------------------------------------------------------
def run_kmeans_clustering(embeddings, n_clusters, random_state=42):
    """
    Runs k-means with k = number of true rule_ids (or pass explicitly).
    Returns predicted cluster labels.
    """
    kmeans = KMeans(n_clusters=n_clusters, random_state=random_state, n_init=10)
    predicted_labels = kmeans.fit_predict(embeddings)
    return predicted_labels, kmeans


# -----------------------------------------------------------------------
# STEP 3: Metric functions
# -----------------------------------------------------------------------
def cluster_purity(true_labels, predicted_labels):
    """
    Computes cluster purity:
    For each predicted cluster, find the most common true label in it,
    sum those counts, divide by total number of points.
    """
    df = pd.DataFrame({"true": true_labels, "pred": predicted_labels})
    total_correct = 0
    for cluster_id in df["pred"].unique():
        subset = df[df["pred"] == cluster_id]
        most_common_count = subset["true"].value_counts().iloc[0]
        total_correct += most_common_count
    return total_correct / len(df)


def compute_ari(true_labels, predicted_labels):
    """Adjusted Rand Index between true rule_id labels and predicted clusters."""
    return adjusted_rand_score(true_labels, predicted_labels)


def compute_nmi(true_labels, predicted_labels):
    """
    Normalized Mutual Information — an optional companion metric to ARI.
    Useful because it's less sensitive to cluster size imbalance than ARI.
    """
    return normalized_mutual_info_score(true_labels, predicted_labels)


def per_rule_purity_breakdown(true_labels, predicted_labels, clause_refs):
    """
    Diagnostic breakdown: for each TRUE rule_id, what fraction of its clauses
    ended up in the single dominant predicted cluster for that rule_id.
    Helps identify which specific rules are poorly separated in embedding space.
    """
    df = pd.DataFrame({
        "clause_ref": clause_refs,
        "true": true_labels,
        "pred": predicted_labels
    })

    breakdown = []
    for rule_id in sorted(df["true"].unique()):
        subset = df[df["true"] == rule_id]
        dominant_cluster_count = subset["pred"].value_counts().iloc[0]
        cohesion = dominant_cluster_count / len(subset)
        breakdown.append({
            "rule_id": rule_id,
            "num_clauses": len(subset),
            "cohesion": round(cohesion, 3),
        })

    return pd.DataFrame(breakdown).sort_values("cohesion")


# -----------------------------------------------------------------------
# STEP 4: Aggregate function
# -----------------------------------------------------------------------
def evaluate_clustering_coherence(clauses, n_clusters=None, random_state=42):
    """
    Full pipeline: prepares data, runs k-means, computes purity/ARI/NMI,
    and returns per-rule cohesion breakdown.
    """
    embeddings, true_labels, clause_refs = prepare_clustering_data(clauses)

    if n_clusters is None:
        n_clusters = len(set(true_labels))  # default: match number of rule_ids

    predicted_labels, kmeans_model = run_kmeans_clustering(
        embeddings, n_clusters, random_state
    )

    purity = cluster_purity(true_labels, predicted_labels)
    ari = compute_ari(true_labels, predicted_labels)
    nmi = compute_nmi(true_labels, predicted_labels)
    breakdown_df = per_rule_purity_breakdown(true_labels, predicted_labels, clause_refs)

    results = {
        "n_clusters": n_clusters,
        "purity": round(purity, 4),
        "ARI": round(ari, 4),
        "NMI": round(nmi, 4),
        "worst_5_rules_by_cohesion": breakdown_df.head(5).to_dict("records"),
        "best_5_rules_by_cohesion": breakdown_df.tail(5).to_dict("records"),
    }

    return results, true_labels, predicted_labels, embeddings, breakdown_df


# -----------------------------------------------------------------------
# STEP 5: Visualization
# -----------------------------------------------------------------------
def visualize_clusters(embeddings, true_labels, predicted_labels,
                        method="pca", title_prefix="", figsize=(16, 7)):
    """
    Reduces embeddings to 2D and plots side-by-side:
      Left  = colored by TRUE rule_id (ground truth structure)
      Right = colored by PREDICTED cluster (what the model actually grouped)

    method: "pca" (fast, linear) or "tsne" (slower, often clearer separation)
    """
    if method == "pca":
        reducer = PCA(n_components=2, random_state=42)
    elif method == "tsne":
        reducer = TSNE(n_components=2, random_state=42, perplexity=30, init="pca")
    else:
        raise ValueError("method must be 'pca' or 'tsne'")

    reduced = reducer.fit_transform(embeddings)

    unique_true = sorted(set(true_labels))
    unique_pred = sorted(set(predicted_labels))

    true_color_map = {label: i for i, label in enumerate(unique_true)}
    pred_color_map = {label: i for i, label in enumerate(unique_pred)}

    true_colors = [true_color_map[l] for l in true_labels]
    pred_colors = [pred_color_map[l] for l in predicted_labels]

    fig, axes = plt.subplots(1, 2, figsize=figsize)

    scatter1 = axes[0].scatter(
        reduced[:, 0], reduced[:, 1],
        c=true_colors, cmap="tab20", s=25, alpha=0.8
    )
    axes[0].set_title(f"{title_prefix} — Colored by TRUE rule_id ({len(unique_true)} rules)")
    axes[0].set_xlabel("Component 1")
    axes[0].set_ylabel("Component 2")

    scatter2 = axes[1].scatter(
        reduced[:, 0], reduced[:, 1],
        c=pred_colors, cmap="tab20", s=25, alpha=0.8
    )
    axes[1].set_title(f"{title_prefix} — Colored by PREDICTED cluster ({len(unique_pred)} clusters)")
    axes[1].set_xlabel("Component 1")
    axes[1].set_ylabel("Component 2")

    plt.tight_layout()
    plt.show()


# -----------------------------------------------------------------------
# USAGE
# -----------------------------------------------------------------------
import json
with open("/Users/himanshu/Documents/Projects/policy-and-compliance-reasoning/data/finra_clauses_embedded__voyage-law-2.jsonl", "r") as f:
    clauses = [json.loads(line) for line in f]

results, true_labels, predicted_labels, embeddings, breakdown_df = evaluate_clustering_coherence(clauses, n_clusters=37)

print(json.dumps(results, indent=2, default=str))

print("\nFull per-rule cohesion breakdown:")
print(breakdown_df.to_string(index=False))

# Visualize — try PCA first (fast), then t-SNE for clearer separation
visualize_clusters(embeddings, true_labels, predicted_labels, method="pca", title_prefix="voyage-law-2")
visualize_clusters(embeddings, true_labels, predicted_labels, method="tsne", title_prefix="voyage-law-2")