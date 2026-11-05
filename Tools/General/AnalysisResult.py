# -*- coding: utf-8 -*-
"""
Created on Wed Apr 22 14:22:22 2026

@author: Charlie.Jaouen
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import re
import pandas as pd
import pickle
import os

from matplotlib.figure import Figure

def _safe_filename(text: str, fallback: str = "figure", max_len: int = 180) -> str:
    text = str(text)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r'[\\/:*?"<>|]', "_", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.rstrip(". ")
    text = text.replace(" ", "_")
    text = text[:max_len]
    return text or fallback


def _safe_sheet_name(name, fallback="Sheet1", max_len=31):
    name = str(name).strip() if name is not None else ""
    invalid = ['[', ']', ':', '*', '?', '/', '\\']
    for ch in invalid:
        name = name.replace(ch, "_")
    name = name[:max_len]
    return name if name else fallback


def _unique_sheet_name(base_name, used_names, max_len=31):
    if base_name not in used_names:
        used_names.add(base_name)
        return base_name

    k = 2
    while True:
        suffix = f"_{k}"
        candidate = base_name[:max_len - len(suffix)] + suffix
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
        k += 1


@dataclass
class AnalysisResult:
    figures: list[Figure] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    log: list[dict] = field(default_factory=list)
    saving_path: Path | None = None

    def add(self, fig: Figure | None, data=None):
        if fig is not None:
            self.figures.append(fig)
            fig_idx = len(self.figures)  # 1-based index to match save()

            if data is not None:
                self.data[fig_idx] = data

    def reset_figures(self):
        self.figures = []
        self.data = {}

    def add_log(self, message: str, level: str = "INFO", **extra):
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "level": str(level).upper(),
            "message": str(message),
        }
        if extra:
            entry.update(extra)
        self.log.append(entry)

    def extend_log(self, entries):
        for entry in entries:
            if isinstance(entry, dict):
                self.log.append(entry)

    def _get_title(self, fig: Figure, i: int):
        if fig._suptitle:
            title = fig._suptitle.get_text()
        elif fig.axes and fig.axes[0].get_title():
            title = fig.axes[0].get_title()
        else:
            title = f"figure_{i}"

        axes_titles = [ax.get_title() for ax in fig.axes]
        return _safe_filename(title, fallback=f"figure_{i}"), axes_titles

    def save_logs(self, folder=None, *, text_log=None):
        """Save the readable GUI analysis log."""
        folder = Path(folder) if folder is not None else self.saving_path
        if folder is None:
            raise ValueError("A folder is required before analysis logs can be saved.")
        folder.mkdir(parents=True, exist_ok=True)

        text_log_path = folder / "analysis_log.txt"
        if text_log is not None:
            text = str(text_log).rstrip() + "\n"
            with open(text_log_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)

        return {"text_log": str(text_log_path)}

    def save(self, folder, fmt="pdf", *, save_logs=True, text_log=None):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)

        saved_figures = []
        saved_data = []

        for i, fig in enumerate(self.figures, start=1):
            name, axes_titles = self._get_title(fig, i)

            # Save figure
            fig_path = folder / f"{name}.{fmt}"
            fig.savefig(fig_path, bbox_inches="tight")
            saved_figures.append(str(fig_path))

            # Save associated data
            if i in self.data:
                current_items = self.data[i]
                if not isinstance(current_items, (list, tuple)):
                    current_items = [current_items]

                dataframes = []
                other_objects = []

                for idx, current_data in enumerate(current_items, start=1):
                    if isinstance(current_data, pd.DataFrame):
                        dataframes.append(current_data)
                    else:
                        other_objects.append((idx, current_data))

                # Save all DataFrames in one Excel file, one sheet per axis
                if dataframes:
                    xlsx_path = folder / f"{name}.xlsx"
                    used_sheet_names = set()

                    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
                        for ax_idx, df in enumerate(dataframes):
                            if ax_idx < len(axes_titles) and axes_titles[ax_idx]:
                                sheet_name = axes_titles[ax_idx]
                            else:
                                sheet_name = f"ax_{ax_idx + 1}"

                            sheet_name = _safe_sheet_name(
                                sheet_name,
                                fallback=f"ax_{ax_idx + 1}"
                            )
                            sheet_name = _unique_sheet_name(sheet_name, used_sheet_names)

                            df.to_excel(writer, sheet_name=sheet_name, index=False)

                    saved_data.append(str(xlsx_path))

                # Save non-DataFrame objects as pickle
                for obj_idx, current_data in other_objects:
                    pkl_path = folder / f"{name}_data_{obj_idx}.pkl"
                    with open(pkl_path, "wb") as f:
                        pickle.dump(current_data, f)
                    saved_data.append(str(pkl_path))

        self.saving_path = folder

        log_paths = {"text_log": str(folder / "analysis_log.txt")}
        if save_logs:
            log_paths = self.save_logs(folder, text_log=text_log)

        return {
            "figures": saved_figures,
            "data": saved_data,
            **log_paths,
        }

    def __bool__(self):
        return bool(self.figures) or bool(self.log)
