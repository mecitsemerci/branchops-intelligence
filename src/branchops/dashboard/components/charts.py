"""ECharts option builders (ECharts is bundled with NiceGUI and served locally)."""

from __future__ import annotations

from typing import Any

PALETTE = ["#2563eb", "#16a34a", "#f59e0b", "#dc2626", "#7c3aed", "#0891b2", "#db2777", "#65a30d"]


def trend_chart(daily: list[dict[str, Any]]) -> dict[str, Any]:
    days = [d["day"] for d in daily]
    return {
        "color": PALETTE,
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["Kayıt", "Ort. tamlık", "Ort. kalite"]},
        "xAxis": {"type": "category", "data": days},
        "yAxis": [
            {"type": "value", "name": "Kayıt"},
            {"type": "value", "name": "Puan", "min": 0, "max": 100},
        ],
        "series": [
            {"name": "Kayıt", "type": "bar", "data": [d["issues"] for d in daily]},
            {
                "name": "Ort. tamlık",
                "type": "line",
                "yAxisIndex": 1,
                "data": [d["avg_completeness"] for d in daily],
            },
            {
                "name": "Ort. kalite",
                "type": "line",
                "yAxisIndex": 1,
                "data": [d["avg_quality"] for d in daily],
            },
        ],
    }


def pie_chart(items: list[dict[str, Any]], name_key: str, value_key: str) -> dict[str, Any]:
    return {
        "color": PALETTE,
        "tooltip": {"trigger": "item"},
        "series": [
            {
                "type": "pie",
                "radius": ["40%", "70%"],
                "data": [{"name": i[name_key], "value": i[value_key]} for i in items],
            }
        ],
    }


def bar_chart(
    labels: list[str], values: list[Any], name: str, horizontal: bool = False, max_value: float | None = None
) -> dict[str, Any]:
    cat_axis = {"type": "category", "data": labels}
    val_axis: dict[str, Any] = {"type": "value"}
    if max_value is not None:
        val_axis["max"] = max_value
    return {
        "color": PALETTE,
        "tooltip": {"trigger": "axis"},
        "grid": {"containLabel": True},
        "xAxis": val_axis if horizontal else cat_axis,
        "yAxis": cat_axis if horizontal else val_axis,
        "series": [{"name": name, "type": "bar", "data": values}],
    }


def history_chart(history: list[dict[str, Any]]) -> dict[str, Any]:
    labels = [h["started_at"][:16].replace("T", " ") for h in history]
    return {
        "color": PALETTE,
        "tooltip": {"trigger": "axis"},
        "legend": {"data": ["Tamlık", "Kalite"]},
        "xAxis": {"type": "category", "data": labels},
        "yAxis": {"type": "value", "min": 0, "max": 100},
        "series": [
            {"name": "Tamlık", "type": "line", "data": [h["completeness"] for h in history]},
            {"name": "Kalite", "type": "line", "data": [h["quality"] for h in history]},
        ],
    }
