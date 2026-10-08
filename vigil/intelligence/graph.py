"""Graph / network analytics on the equity universe.

Nodes  : instruments.  Edges: |rolling return correlation| above a data-driven threshold.
Outputs: communities (greedy modularity), centrality, sector-vs-community agreement, and
         correlation-structure change between two windows (the 'correlation breakdown' signal
         consumed by the case engine and the Decision Gate's market-stability input).
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

import networkx as nx
import numpy as np
import pandas as pd

from ..config import VigilConfig, load_config
from ..logging_utils import get_logger
from ..storage.lake import DataLake

log = get_logger("vigil.intelligence.graph")


def _returns_matrix(cfg: VigilConfig, window: int, as_of: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    feats = DataLake(cfg).read("features", "equity_features")[["date", "symbol", "ret_1"]]
    if as_of is not None:
        feats = feats[feats["date"] <= as_of]          # point-in-time slice
    wide = feats.pivot_table(index="date", columns="symbol", values="ret_1").dropna(how="all")
    return wide.tail(window)


def build_correlation_graph(cfg: Optional[VigilConfig] = None, window: int = 120,
                            as_of: Optional[pd.Timestamp] = None,
                            quantile: float = 0.70) -> Tuple[nx.Graph, pd.DataFrame]:
    cfg = cfg or load_config()
    wide = _returns_matrix(cfg, window, as_of)
    corr = wide.corr(min_periods=max(20, window // 3))
    values = corr.where(~np.eye(len(corr), dtype=bool)).stack()
    threshold = float(np.nanquantile(values.abs(), quantile))
    g = nx.Graph()
    for sym in corr.columns:
        g.add_node(sym, sector=cfg.sector_of(sym), name=cfg.name_of(sym))
    for (a, b), val in values.items():
        if a < b and abs(val) >= threshold:
            g.add_edge(a, b, weight=round(float(abs(val)), 4), correlation=round(float(val), 4))
    g.graph["threshold"] = round(threshold, 4)
    g.graph["window"] = window
    return g, corr


def analyse_graph(g: nx.Graph, cfg: VigilConfig) -> Dict:
    if g.number_of_edges() == 0:
        return {"nodes": g.number_of_nodes(), "edges": 0, "communities": [], "note": "no edges above threshold"}
    communities = list(nx.algorithms.community.greedy_modularity_communities(g, weight="weight"))
    modularity = nx.algorithms.community.modularity(g, communities, weight="weight")
    # eigenvector centrality is only well defined per connected component
    eig: Dict[str, float] = {}
    for component in nx.connected_components(g):
        sub = g.subgraph(component)
        if sub.number_of_nodes() == 1:
            eig[next(iter(component))] = 0.0
            continue
        try:
            eig.update(nx.eigenvector_centrality_numpy(sub, weight="weight"))
        except Exception:
            eig.update({n: float(d) for n, d in sub.degree(weight="weight")})
    btw = nx.betweenness_centrality(g, weight="weight")
    deg = dict(g.degree(weight="weight"))
    comm_of = {n: i for i, com in enumerate(communities) for n in com}
    sector_purity = []
    for i, com in enumerate(communities):
        sectors = [cfg.sector_of(n) for n in com]
        top = max(set(sectors), key=sectors.count)
        sector_purity.append({"community": i, "size": len(com), "dominant_sector": top,
                              "purity": round(sectors.count(top) / len(com), 3),
                              "members": sorted(com)})
    nodes = [{
        "symbol": n, "name": cfg.name_of(n), "sector": cfg.sector_of(n),
        "community": comm_of.get(n, -1),
        "degree_strength": round(float(deg.get(n, 0.0)), 4),
        "eigen_centrality": round(float(eig.get(n, 0.0)), 4),
        "betweenness": round(float(btw.get(n, 0.0)), 4),
    } for n in g.nodes]
    edges = [{"source": a, "target": b, "weight": d["weight"], "correlation": d["correlation"]}
             for a, b, d in g.edges(data=True)]
    return {
        "nodes": nodes, "edges": edges,
        "n_nodes": g.number_of_nodes(), "n_edges": g.number_of_edges(),
        "threshold": g.graph.get("threshold"), "window": g.graph.get("window"),
        "density": round(nx.density(g), 4), "modularity": round(float(modularity), 4),
        "communities": sector_purity,
        "most_central": max(nodes, key=lambda n: n["eigen_centrality"])["symbol"],
    }


def correlation_change(cfg: Optional[VigilConfig] = None, window: int = 60) -> Dict:
    """Compare the latest window against the preceding one — 'correlation breakdown' detector."""
    cfg = cfg or load_config()
    wide = _returns_matrix(cfg, window * 2)
    if len(wide) < window * 2 - 5:
        return {"available": False, "reason": "insufficient history"}
    recent = wide.tail(window).corr()
    prior = wide.head(len(wide) - window).corr()
    delta = (recent - prior).abs()
    mask = ~np.eye(len(delta), dtype=bool)
    flat = delta.where(mask).stack().sort_values(ascending=False)
    mean_recent = float(recent.where(mask).stack().mean())
    mean_prior = float(prior.where(mask).stack().mean())
    pairs = [{"pair": f"{a} / {b}", "delta": round(float(v), 3),
              "recent": round(float(recent.loc[a, b]), 3), "prior": round(float(prior.loc[a, b]), 3)}
             for (a, b), v in flat.head(6).items() if a < b]
    return {
        "available": True, "window": window,
        "mean_correlation_recent": round(mean_recent, 4),
        "mean_correlation_prior": round(mean_prior, 4),
        "shift": round(mean_recent - mean_prior, 4),
        "state": "CORRELATION_SPIKE" if mean_recent - mean_prior > 0.12 else
                 "CORRELATION_BREAKDOWN" if mean_recent - mean_prior < -0.12 else "STABLE",
        "largest_changes": pairs,
    }


def run_graph_analytics(cfg: Optional[VigilConfig] = None) -> Dict:
    cfg = cfg or load_config()
    g, _ = build_correlation_graph(cfg)
    result = analyse_graph(g, cfg)
    result["correlation_change"] = correlation_change(cfg)
    result["interpretation"] = (
        "Communities are discovered from price co-movement only. Where a community does not match "
        "the sector label, the market is pricing a shared factor that is not sectoral — VIGIL uses "
        "this as context, never as a causal claim.")
    out = cfg.reports_root / "results" / "graph.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    log.info("graph: %d nodes %d edges modularity=%.3f communities=%d",
             result["n_nodes"], result["n_edges"], result["modularity"], len(result["communities"]))
    return result
