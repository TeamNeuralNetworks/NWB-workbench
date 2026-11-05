# -*- coding: utf-8 -*-
"""
Created on Thu Aug 22 18:06:38 2024

@author: Pierre.LE-CABEC
"""
import math
import importlib
import numpy as np
import matplotlib

import matplotlib.pyplot as plt
from matplotlib.patches import PathPatch
import tkinter as tk
import tkinter.ttk as ttk
from tkinter import filedialog
from tkinter import messagebox
from Tools.General.AnalysisResult import AnalysisResult

def get_plot_number_as_letter(indx):
    if indx == 0:
        return 'i.'
    elif indx == 1:
        return 'ii.'
    elif indx == 2:
        return 'iii.'
    elif indx == 3:
        return 'iv.'
    elif indx == 4:
        return 'v.'
    elif indx == 5:
        return 'vi.'
    elif indx == 6:
        return 'vii.'
    elif indx == 7:
        return 'viii.'
    elif indx == 8:
        return 'ix.'
    elif indx == 9:
        return 'x.'

def add_mean_median_boxplot(ax, metric_df, group_label, metric_label, display_mean=True, display_median=True, palette_dict=None, text_color='k', display_line=True, zorder=None):
    means = metric_df.groupby(group_label)[metric_label].mean().reset_index()
    median = metric_df.groupby(group_label)[metric_label].median().reset_index()
    tick_label_position = ax.get_xticklabels()
    for (index, row_mean), (index, row_median) in zip(means.iterrows(), median.iterrows()):
        for current_tick_label in tick_label_position:
            if current_tick_label.get_text() == str(row_mean[group_label]):
                current_position = current_tick_label.get_position()[0]
        
        if display_mean:
            ax.text(current_position, row_mean[metric_label], f'mean: {row_mean[metric_label]:.2f}', color=text_color, ha='center', va='bottom', fontweight='bold', zorder=zorder)
        if display_median:
            ax.text(current_position, row_median[metric_label], f'median: {row_median[metric_label]:.2f}', color=text_color, ha='center', va='top', fontweight='bold', zorder=zorder)
        
        if display_line:
            if palette_dict is None:
                color = None
            else:
                color = palette_dict[str(row_mean[group_label])]
            ax.axhline(row_mean[metric_label], color=color, linestyle='--', zorder=zorder)
    

def color_line_boxplot(ax, pallet_dict=None, data=None, x=None, y=None, hue=None, median_color='white', base_color=None, median_width=3, flier_size=10, whisker_caps_width=2):
    
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    
    x_order = ax.get_xticklabels()
    x_order_text = [text.get_text() for text in x_order]

    if hue is not None:
        hue_order_text = data[hue].unique()
    
    if hue is None:
        hue_order_text = [None]
    
    indx_line = 0
    indx_patch = 0
    for hue_label in hue_order_text:
        for x_label in x_order_text:
            if hue is None:
                label = x_label
            else:
                label = f'{x_label} {hue_label}'
            
            if pallet_dict is None:
                color = None
            else:
                try:
                    if hue is None:
                        color = pallet_dict[x_label]
                    else:
                        color = pallet_dict[f'{x_label}_{hue_label}']
                except KeyError:
                    color = base_color
            
            current_line_set = ax.lines[indx_line: indx_line+6]
            
            current_line_set[5].set_markersize(flier_size)
            if color is not None:
                current_line_set[5].set_markeredgecolor(color)
                current_line_set[5].set_markerfacecolor(color)
            
            current_line_set[4].set_c(median_color)#set medial color
            current_line_set[4].set_linewidth(median_width)
            
            for whisker_caps in current_line_set[:4]:
                if color is not None:
                    whisker_caps.set_c(color)
                whisker_caps.set_linewidth(whisker_caps_width)
            
            boxes = [patch for patch in ax.get_children() if isinstance(patch, PathPatch)][indx_patch]
            if color is not None:
                boxes.set_facecolor(color)
            boxes.set_label(label)
            boxes.set_linewidth(0)
            
            indx_line += 6
            indx_patch += 1
            
    if ax.legend_ is not None:
        ax.legend_.remove()  # Removes the legend




def add_band_to_plot(ax, reward_centered, pre_reward_duration, delay=None, plot_cues=True, plot_stim=False, alpha=0.2):
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.set_xlim(ax.dataLim.x0, ax.dataLim.x1)
    if not reward_centered:
        if plot_cues:
            ax.axvspan(0, 0.5, color='green', alpha=alpha+0.1, linewidth=0)
            ax.axvspan(1.5, 2, color='green', alpha=alpha+0.1, linewidth=0)
            
        if delay is not None:
            if delay == 'Fixed Delay' or delay == 'all':
                ax.axvspan(2.5, 2.65, color='red', alpha=alpha, linewidth=0)
            if delay == 'Random Delay' or delay == 'training' or delay == 'all':
                if pre_reward_duration != 'all':
                    ax.axvspan(2+(pre_reward_duration/1000), 2.15+(pre_reward_duration/1000), color='red', alpha=alpha, linewidth=0)
                else:
                    ax.axvspan(2.4, 2.55, color='red', alpha=alpha, linewidth=0)
                    ax.axvspan(2.9, 3.05, color='red', alpha=alpha, linewidth=0)
        if plot_stim: 
            ax.axvspan(1.5, 2.295, 0, 0.05, color='blue', alpha=alpha+0.3, linewidth=0)
    else:
        ax.axvspan(0, 0.15, color='red', alpha=alpha, linewidth=0)
        
def sliding_mean(data, window_size):
    # Pad the data to handle edge cases
    padded_data = np.pad(data, (window_size // 2, window_size // 2), mode='edge')

    # Use convolution to calculate the sliding mean
    weights = np.ones(window_size) / window_size
    sliding_mean_result = np.convolve(padded_data, weights, mode='valid')

    return sliding_mean_result



class PlottingToolboxGUI:
    def __init__(self, result=None, layer=1, nb_fig_layer=4, nb_of_row=2,
                 change_lim_window=True, xlim=(None, None), ylim=(None, None)):
        self._closed = False
        self._qt_tk_timer = None
        self._foreground_request = 0
        
        if not self._can_use_gui():
            print("Notebook mode detected â†’ using widget interface")
            self._init_notebook_mode(layer, result, nb_fig_layer)
            return

        self._init_gui_mode(layer, result, nb_fig_layer, nb_of_row,
                            change_lim_window, xlim, ylim)

    # ------------------------------------------------------------------ #
    #  ENV DETECTION
    # ------------------------------------------------------------------ #
    def _can_use_gui(self):

        backend = matplotlib.get_backend().lower()

        try:
            from IPython import get_ipython
            ip = get_ipython()
            in_notebook = ip is not None and "IPKernelApp" in ip.config
        except Exception:
            in_notebook = False

        gui_backends = ["qt", "tk"]

        if in_notebook and not any(b in backend for b in gui_backends):
            return False

        return True

    # ------------------------------------------------------------------ #
    #  GUI MODE (your original logic, slightly wrapped)
    # ------------------------------------------------------------------ #
    def _init_gui_mode(self, layer, result, nb_fig_layer, nb_of_row,
                      change_lim_window, xlim, ylim):

        explicit_figures = getattr(result, "figures", None) if result is not None else None
        if explicit_figures is None:
            figures = [plt.figure(num) for num in plt.get_fignums()]
        else:
            figures = list(explicit_figures or [])
        self.figures = self._unique_figures(figures)
        if result is None:
            self.result = AnalysisResult()
            for fig in self.figures:
                self.result.add(fig)
        else: 
            self.result = result
            
        self.layer = layer
        self.nb_fig_layer = nb_fig_layer
        self.nb_of_row = nb_of_row

        self.axis_dict = {}
        self.current_figures = []

        self.tk = tk
        self.ttk = ttk

        self.build_gui(change_lim_window)

    # ------------------------------------------------------------------ #
    #  NOTEBOOK MODE
    # ------------------------------------------------------------------ #
    def _init_notebook_mode(self, layer, result, nb_fig_layer):
        import ipywidgets as widgets
        from IPython.display import display

        explicit_figures = getattr(result, "figures", None) if result is not None else None
        if explicit_figures is None:
            figures = [plt.figure(num) for num in plt.get_fignums()]
        else:
            figures = list(explicit_figures or [])
        self.figures = self._unique_figures(figures)
        self.result = result if result is not None else AnalysisResult(figures=list(self.figures))
        self.layer = layer
        self.nb_fig_layer = nb_fig_layer

        # controls
        self.prev_btn = widgets.Button(description="Previous")
        self.next_btn = widgets.Button(description="Next")

        self.prev_btn.on_click(self._nb_prev)
        self.next_btn.on_click(self._nb_next)

        # limits
        self.xmin = widgets.FloatText(description="xmin")
        self.xmax = widgets.FloatText(description="xmax")
        self.ymin = widgets.FloatText(description="ymin")
        self.ymax = widgets.FloatText(description="ymax")

        self.apply_btn = widgets.Button(description="Apply limits")
        self.apply_btn.on_click(self._nb_apply_limits)
        
        self.save_btn = widgets.Button(description="Save figures")
        self.save_btn.on_click(self._save_fig)

        self.output = widgets.Output()

        display(widgets.HBox([self.prev_btn, self.next_btn]))
        display(widgets.HBox([self.xmin, self.xmax, self.ymin, self.ymax]))
        display(self.apply_btn)
        display(self.output)

        self._nb_show()

    # ------------------------------------------------------------------ #
    #  NOTEBOOK NAVIGATION
    # ------------------------------------------------------------------ #
    def _nb_show(self):
        from IPython.display import display

        self.output.clear_output()

        start = (self.layer - 1) * self.nb_fig_layer
        end = self.layer * self.nb_fig_layer

        with self.output:
            for fig in self.figures[start:end]:
                display(fig)
    
    @staticmethod
    def _unique_figures(figures):
        """Keep the supplied figure order while removing duplicate objects."""
        unique = []
        seen = set()
        for fig in list(figures or []):
            if fig is None or id(fig) in seen:
                continue
            seen.add(id(fig))
            unique.append(fig)
        return unique

    @staticmethod
    def _unused_figure_path(folder, stem, suffix=".pdf", reserved=None):
        """Return a non-existing, case-insensitively unique output path."""
        from pathlib import Path

        folder = Path(folder)
        reserved = reserved if reserved is not None else set()
        candidate = folder / f"{stem}{suffix}"
        index = 2
        while candidate.name.casefold() in reserved or candidate.exists():
            candidate = folder / f"{stem}_{index}{suffix}"
            index += 1
        reserved.add(candidate.name.casefold())
        return candidate

    def _save_fig(self, *_args):
        """Save the explicit Plot GUI figure set, including pipeline results."""
        figures = self._unique_figures(getattr(self, "figures", []))
        if not figures:
            try:
                messagebox.showwarning(
                    "Save figures",
                    "No figures are available to save.",
                    parent=getattr(self, "root", None),
                )
            except Exception:
                pass
            return

        root = getattr(self, "root", None)
        try:
            if root is not None:
                root.deiconify()
                root.attributes("-topmost", True)
                root.lift()
                root.focus_force()
                root.update_idletasks()
        except Exception:
            pass

        dialog_options = {
            "title": "Select a folder for the figures",
        }
        if root is not None:
            dialog_options["parent"] = root
        saving_path = getattr(getattr(self, "result", None), "saving_path", None)
        if saving_path:
            try:
                from pathlib import Path
                initial_path = Path(saving_path)
                if initial_path.exists():
                    dialog_options["initialdir"] = str(initial_path)
            except Exception:
                pass

        try:
            folder_path = filedialog.askdirectory(**dialog_options)
        except Exception as exc:
            try:
                messagebox.showerror(
                    "Save figures",
                    f"Could not open the folder selector:\n\n{exc}",
                    parent=root,
                )
            except Exception:
                pass
            return
        finally:
            try:
                if root is not None:
                    root.attributes("-topmost", False)
            except Exception:
                pass

        if not folder_path:
            self._bring_plot_gui_to_front()
            return

        try:
            from pathlib import Path

            folder = Path(folder_path)
            folder.mkdir(parents=True, exist_ok=True)
            title_helper = AnalysisResult()
            reserved = set()
            saved_paths = []
            for index, fig in enumerate(figures, start=1):
                stem, _axes_titles = title_helper._get_title(fig, index)
                output_path = self._unused_figure_path(
                    folder,
                    stem,
                    suffix=".pdf",
                    reserved=reserved,
                )
                fig.savefig(output_path, bbox_inches="tight")
                saved_paths.append(output_path)

            # Remember the chosen directory when the supplied result supports
            # it, without requiring the pipeline's lightweight result wrapper
            # to implement the complete AnalysisResult API.
            try:
                self.result.saving_path = folder
            except Exception:
                pass

            messagebox.showinfo(
                "Figures saved",
                f"Saved {len(saved_paths)} figure(s) in:\n{folder}",
                parent=root,
            )
        except Exception as exc:
            try:
                messagebox.showerror(
                    "Save figures",
                    f"Could not save the figures:\n\n{exc}",
                    parent=root,
                )
            except Exception:
                pass
        finally:
            self._bring_plot_gui_to_front()
        
            
    def _nb_prev(self, _):
        if self.layer > 1:
            self.layer -= 1
            self._nb_show()

    def _nb_next(self, _):
        max_layer = (len(self.figures) + self.nb_fig_layer - 1) // self.nb_fig_layer
        if self.layer < max_layer:
            self.layer += 1
            self._nb_show()

    def _nb_apply_limits(self, _):
        xmin = self.xmin.value
        xmax = self.xmax.value
        ymin = self.ymin.value
        ymax = self.ymax.value

        start = (self.layer - 1) * self.nb_fig_layer
        end = self.layer * self.nb_fig_layer

        for fig in self.figures[start:end]:
            for ax in fig.get_axes():
                if xmin is not None and xmax is not None:
                    ax.set_xlim(xmin, xmax)
                if ymin is not None and ymax is not None:
                    ax.set_ylim(ymin, ymax)

        self._nb_show()


    # ------------------------------------------------------------------ #
    #  Figure handling
    # ------------------------------------------------------------------ #
    def _move_figures_on_geometry(self, figures, screen_geo, layout_slots=None):
        screen_x = screen_geo["x"]
        screen_y = screen_geo["y"]
        screen_width = screen_geo["width"]
        screen_height = screen_geo["height"]
    
        if not figures:
            return
    
        # Keep the requested layout, even if fewer figures are visible.
        # Example: rows=3 and figs/layer=20 still computes the grid for 20 slots.
        if layout_slots is None:
            layout_slots = self.nb_fig_layer
    
        ncols = math.ceil(layout_slots / self.nb_of_row)
        nrows = math.ceil(layout_slots / ncols)
    
        fig_width = max(1, screen_width // ncols)
        fig_height = max(1, screen_height // nrows)
    
        for i, fig in enumerate(figures):
            fig_x = screen_x + (i % ncols) * fig_width
            fig_y = screen_y + (i // ncols) * fig_height
    
            manager = fig.canvas.manager
            window = manager.window
    
            try:
                window.show()
                window.raise_()
            except Exception:
                pass
    
            try:
                window.move(fig_x, fig_y)
            except Exception:
                pass
    
            fig.set_size_inches(
                fig_width / 100,
                max(1, fig_height / 96 - 0.8),
                forward=True
            )
    
            try:
                fig.tight_layout()
            except Exception:
                pass
    
            fig.canvas.draw()
            fig.canvas.flush_events()
    
            try:
                window.repaint()
            except Exception:
                pass
        
        
    def _move_figures_all_screens_separate(self):
        screens = self.screens
        n_screens = len(screens)
    
        if n_screens <= 1:
            self._move_figures_on_geometry(
                self.current_figures,
                screens[0],
                layout_slots=self.nb_fig_layer,
            )
            return
    
        figs_per_screen = math.ceil(self.nb_fig_layer / n_screens)
    
        for screen_i, screen_geo in enumerate(screens):
            start = screen_i * figs_per_screen
            end = start + figs_per_screen
            figs = self.current_figures[start:end]
    
            self._move_figures_on_geometry(
                figs,
                screen_geo,
                layout_slots=figs_per_screen,
            )
    def move_figures(self):
        """Position figures belonging to the active layer on the selected screen mode."""
        if not self.figures:
            return
    
        screens = getattr(self, "screens", None) or self._get_available_screens()
        self.screens = screens
    
        selected_text = self.screen_combo.get() if hasattr(self, "screen_combo") else ""
    
        start_ind = (self.layer - 1) * self.nb_fig_layer
        end_ind = self.layer * self.nb_fig_layer
        self.current_figures = self.figures[start_ind:end_ind]
    
        if selected_text == "All screens separate" and len(screens) > 1:
            self._move_figures_all_screens_separate()
        else:
            screen_geo = self._get_selected_screen_geometry()
            self._move_figures_on_geometry(
                self.current_figures,
                screen_geo,
                layout_slots=self.nb_fig_layer,
            )
    
        self.update_axis_list()
            
    def _get_available_screens(self):
        screens = []
    
        try:
            if self.figures:
                manager = self.figures[0].canvas.manager
                window = manager.window
    
                app = window.windowHandle().screen().virtualSiblings()
                for i, screen in enumerate(app):
                    geo = screen.geometry()
                    screens.append({
                        "label": f"Screen {i + 1}",
                        "x": int(geo.x()),
                        "y": int(geo.y()),
                        "width": int(geo.width()),
                        "height": int(geo.height()),
                    })
        except Exception:
            pass
    
        if not screens:
            try:
                screens.append({
                    "label": "Screen 1",
                    "x": 0,
                    "y": 0,
                    "width": int(self.root.winfo_screenwidth()),
                    "height": int(self.root.winfo_screenheight()),
                })
            except Exception:
                screens.append({
                    "label": "Screen 1",
                    "x": 0,
                    "y": 0,
                    "width": 1200,
                    "height": 800,
                })
    
        return screens
    
    def _get_selected_screen_geometry(self):
        """
        Return geometry selected in the combobox.
    
        'All screens merged' returns one virtual bounding rectangle.
        'All screens separate' is handled directly inside move_figures().
        """
        screens = getattr(self, "screens", None) or self._get_available_screens()
        self.screens = screens
    
        selected_text = self.screen_combo.get() if hasattr(self, "screen_combo") else ""
    
        if selected_text == "All screens merged" and len(screens) > 1:
            min_x = min(s["x"] for s in screens)
            min_y = min(s["y"] for s in screens)
            max_x = max(s["x"] + s["width"] for s in screens)
            max_y = max(s["y"] + s["height"] for s in screens)
    
            return {
                "x": min_x,
                "y": min_y,
                "width": max_x - min_x,
                "height": max_y - min_y,
            }
    
        for s in screens:
            if selected_text == s["label"]:
                return s
    
        return screens[0]

    # ------------------------------------------------------------------ #
    #  Axis-limit logic
    # ------------------------------------------------------------------ #
    def _bring_plot_gui_to_front(self):
        """Raise the Tk toolbox after Qt figure redraw/layout events settle."""
        if self._closed:
            return

        self._foreground_request += 1
        request = self._foreground_request

        def request_is_current():
            if self._closed or request != self._foreground_request:
                return False
            try:
                return bool(self.root.winfo_exists())
            except Exception:
                return False

        def raise_window():
            if not request_is_current():
                return
            try:
                self.root.deiconify()
            except Exception:
                pass
            try:
                # This is temporary. It makes the Tk toolbox win the focus
                # race against Qt Matplotlib windows on Windows.
                self.root.attributes("-topmost", True)
            except Exception:
                pass
            try:
                self.root.lift()
            except Exception:
                pass
            try:
                self.root.focus_force()
            except Exception:
                try:
                    self.root.focus_set()
                except Exception:
                    pass
            try:
                self.root.update_idletasks()
            except Exception:
                pass

        def release_topmost():
            if not request_is_current():
                return
            try:
                self.root.attributes("-topmost", False)
            except Exception:
                pass

        # Raise now, after Tk becomes idle, and after pending Qt paint/window
        # events. The final callback restores normal, non-topmost behaviour.
        raise_window()
        try:
            self.root.after_idle(raise_window)
            self.root.after(75, raise_window)
            self.root.after(175, raise_window)
            self.root.after(300, release_topmost)
        except Exception:
            release_topmost()

    def apply_gui_limits(self):
        try:
            xlim = (
                float(self.xmin_entry.get()) if self.xmin_entry.get() else None,
                float(self.xmax_entry.get()) if self.xmax_entry.get() else None
            )
            ylim = (
                float(self.ymin_entry.get()) if self.ymin_entry.get() else None,
                float(self.ymax_entry.get()) if self.ymax_entry.get() else None
            )
    
            sel = self.axis_combo.get()
            if sel == 'All' or not sel:
                targets = [ax for fig in self.current_figures for ax in fig.get_axes()]
            else:
                targets = self.axis_dict.get(sel, [])
    
            touched_figs = set()
    
            for ax in targets:
                if xlim != (None, None):
                    ax.set_xlim(*xlim)
                if ylim != (None, None):
                    ax.set_ylim(*ylim)
                touched_figs.add(ax.figure)
    
            for fig in touched_figs:
                fig.canvas.draw_idle()
                fig.canvas.flush_events()
    
            plt.pause(0.01)
            self._bring_plot_gui_to_front()
    
        except ValueError:
            print("Invalid number in limits")

    # ------------------------------------------------------------------ #
    #  Navigation / layout callbacks
    # ------------------------------------------------------------------ #
    def go_previous(self):
        max_layer = math.ceil(len(self.figures) / self.nb_fig_layer)
    
        if self.layer > 1:
            self.layer -= 1
    
            try:
                self.move_figures()
            except Exception as e:
                print("move_figures failed:", e)
    
            try:
                self.root.lift()
                self.root.update_idletasks()
            except Exception:
                pass
    
    
    def go_next(self):
        max_layer = math.ceil(len(self.figures) / self.nb_fig_layer)
    
        if self.layer < max_layer:
            self.layer += 1
    
            try:
                self.move_figures()
            except Exception as e:
                print("move_figures failed:", e)
    
            try:
                self.root.lift()
                self.root.update_idletasks()
            except Exception:
                pass

    def update_layout(self):
        try:
            rows = int(self.row_entry.get())
            figs_per_layer = int(self.fig_entry.get())
            if rows > 0 and figs_per_layer > 0:
                self.nb_of_row = rows
                self.nb_fig_layer = figs_per_layer
                self.move_figures()
                self._bring_plot_gui_to_front()
        except ValueError:
            print("Invalid input for rows or figs/layer")

    # ------------------------------------------------------------------ #
    #  Close all
    # ------------------------------------------------------------------ #
    def close_all_figures(self):
        for fig in list(plt.get_fignums()):
            plt.close(fig)

        self.figures = []
        self.current_figures = []
        self.axis_dict.clear()

        self.close()

    def close(self):
        """Close only the Plot GUI, without closing Matplotlib figures."""
        if self._closed:
            return
        self._closed = True

        timer = getattr(self, "_qt_tk_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass
        self._qt_tk_timer = None

        try:
            self.root.destroy()
        except Exception:
            pass

    def _pump_tk_events(self):
        """Process Tk events from the host Qt event loop without blocking it."""
        if self._closed:
            return
        try:
            if not bool(self.root.winfo_exists()):
                self.close()
                return
            self.root.update_idletasks()
            self.root.update()
        except tk.TclError:
            self.close()
        except Exception:
            # A transient redraw error must not stop the host Qt event loop.
            pass

    def _start_event_loop(self):
        """Use the running Qt loop in NWB Workbench; otherwise use Tk normally."""
        for binding_name in ("PySide6", "PyQt6", "PySide2", "PyQt5"):
            try:
                qt_core = importlib.import_module(f"{binding_name}.QtCore")
                app = qt_core.QCoreApplication.instance()
            except Exception:
                continue
            if app is None:
                continue

            timer = qt_core.QTimer()
            timer.setInterval(20)
            timer.timeout.connect(self._pump_tk_events)
            timer.start()
            self._qt_tk_timer = timer
            self._pump_tk_events()
            return

        # Standalone scripts without a Qt application retain the original
        # blocking Tk behavior.
        self.root.mainloop()

    # ------------------------------------------------------------------ #
    #  Axis selector helper
    # ------------------------------------------------------------------ #
    def update_axis_list(self):
        """Rebuild the Combobox with:
           â€¢ 'All'
           â€¢ 'All Ax1', 'All Ax2', ...
           â€¢ 'FigX-AxY' for every visible axis
        """
        self.axis_dict.clear()
        per_axis_index = {}
        labels = []

        for fig in self.current_figures:
            for j, ax in enumerate(fig.get_axes(), start=1):
                ind_label = f"Fig{fig.number}-Ax{j}"
                self.axis_dict[ind_label] = [ax]
                labels.append(ind_label)

                per_axis_index.setdefault(j, []).append(ax)

        group_labels = []
        for idx, axes in sorted(per_axis_index.items()):
            grp_label = f"All Ax{idx}"
            self.axis_dict[grp_label] = axes
            group_labels.append(grp_label)

        self.axis_combo["values"] = ["All"] + group_labels + labels
        self.axis_combo.current(0)

    # ------------------------------------------------------------------ #
    #  GUI construction
    # ------------------------------------------------------------------ #
    def build_gui(self, change_lim_window=False):
        self.root = tk.Tk()
        self.root.title("Figure Navigator")
        self.root.geometry("260x285" if change_lim_window else "260x220")

        self.root.protocol("WM_DELETE_WINDOW", self.close)

        # Navigation
        nav_frame = tk.Frame(self.root)
        nav_frame.pack(pady=5, fill=tk.X)

        tk.Button(nav_frame, text="Previous",
                  command=self.go_previous).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        tk.Button(nav_frame, text="Next",
                  command=self.go_next).pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=5)
        
        # Screen selector
        self.screens = self._get_available_screens()
        
        screen_frame = tk.Frame(self.root)
        screen_frame.pack(pady=5, fill=tk.X)
        
        tk.Label(screen_frame, text="Screen:").pack(side=tk.LEFT)
        
        screen_values = [s["label"] for s in self.screens]

        if len(self.screens) > 1:
            screen_values.append("All screens merged")
            screen_values.append("All screens separate")
        
        self.screen_combo = ttk.Combobox(
            screen_frame,
            state="readonly",
            values=screen_values,
            width=24,
        )
        self.screen_combo.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=5)
        self.screen_combo.current(0)
        self.screen_combo.bind("<<ComboboxSelected>>", lambda _e: self.move_figures())

        # Layout
        layout_frame = tk.Frame(self.root)
        layout_frame.pack(pady=5)
        
        tk.Label(layout_frame, text="Figs/Layer:").grid(row=0, column=2)
        self.fig_entry = tk.Entry(layout_frame, width=5)
        self.fig_entry.insert(0, self.nb_fig_layer)
        self.fig_entry.grid(row=0, column=3)
        
        tk.Label(layout_frame, text="Rows:").grid(row=0, column=0)
        self.row_entry = tk.Entry(layout_frame, width=5)
        self.row_entry.insert(0, self.nb_of_row)
        self.row_entry.grid(row=0, column=1)

        tk.Button(self.root, text="Apply Layout",
                  command=self.update_layout).pack(pady=5)

        # Limits
        limits_frame = tk.Frame(self.root)
        limits_frame.pack(pady=5)

        if change_lim_window:
            tk.Label(limits_frame, text="X min:").grid(row=0, column=0)
            self.xmin_entry = tk.Entry(limits_frame, width=6)
            self.xmin_entry.grid(row=0, column=1)

            tk.Label(limits_frame, text="X max:").grid(row=0, column=2)
            self.xmax_entry = tk.Entry(limits_frame, width=6)
            self.xmax_entry.grid(row=0, column=3)

            tk.Label(limits_frame, text="Y min:").grid(row=1, column=0)
            self.ymin_entry = tk.Entry(limits_frame, width=6)
            self.ymin_entry.grid(row=1, column=1)

            tk.Label(limits_frame, text="Y max:").grid(row=1, column=2)
            self.ymax_entry = tk.Entry(limits_frame, width=6)
            self.ymax_entry.grid(row=1, column=3)

        # Axis selector
        axis_sel_frame = tk.Frame(self.root)
        axis_sel_frame.pack(pady=5, fill=tk.X)

        tk.Label(axis_sel_frame, text="Axis:").pack(side=tk.LEFT)
        self.axis_combo = ttk.Combobox(axis_sel_frame, state="readonly", width=16)
        self.axis_combo.pack(side=tk.RIGHT, fill=tk.X, expand=True)
        
        button_frame = tk.Frame(self.root)
        button_frame.pack(pady=5)
        
        tk.Button(
            button_frame,
            text="Apply Limits",
            command=self.apply_gui_limits
        ).pack(side=tk.LEFT, padx=5)
        
        tk.Button(
            button_frame,
            text="Save figures",
            command=self._save_fig
        ).pack(side=tk.LEFT, padx=5)
        
        tk.Button(
            button_frame,
            text="Close all",
            command=self.close_all_figures
        ).pack(side=tk.LEFT, padx=5)

        self.move_figures()
        self._start_event_loop()


# DELIVERY_BUILD_ID: PTB-20260812-01
