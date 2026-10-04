"""Tkinter session explorer for Formula 1 (data: OpenF1).

Run:  python app.py
"""

import threading
import tkinter as tk
from tkinter import ttk, messagebox

import matplotlib
matplotlib.use('TkAgg')
from matplotlib.backends.backend_tkagg import (
    FigureCanvasTkAgg, NavigationToolbar2Tk)

import f1_data
import plots


class F1App:
    def __init__(self, root):
        self.root = root
        self.root.title('F1 Analytics')
        self.root.geometry('1200x720')

        self.session = None          # loaded f1_data.Session
        self.canvas = None           # FigureCanvasTkAgg
        self.toolbar = None

        f1_data.enable_cache()
        self._build_ui()

    # ---------- layout ----------
    def _build_ui(self):
        controls = ttk.Frame(self.root, padding=10)
        controls.pack(side=tk.LEFT, fill=tk.Y)

        # Season
        ttk.Label(controls, text='Season').pack(anchor='w')
        self.year_var = tk.StringVar()
        self.year_cb = ttk.Combobox(controls, textvariable=self.year_var,
                                    state='readonly', width=24,
                                    values=[str(y) for y in f1_data.get_years()])
        self.year_cb.pack(fill=tk.X)
        self.year_cb.bind('<<ComboboxSelected>>', self._on_year_change)

        # Grand Prix
        ttk.Label(controls, text='Grand Prix').pack(anchor='w', pady=(8, 0))
        self.gp_var = tk.StringVar()
        self.gp_cb = ttk.Combobox(controls, textvariable=self.gp_var,
                                  state='readonly', width=24)
        self.gp_cb.pack(fill=tk.X)

        # Session
        ttk.Label(controls, text='Session').pack(anchor='w', pady=(8, 0))
        self.session_var = tk.StringVar(value='R')
        ttk.Combobox(controls, textvariable=self.session_var, state='readonly',
                     width=24, values=f1_data.SESSION_TYPES).pack(fill=tk.X)

        self.load_btn = ttk.Button(controls, text='Load session',
                                   command=self._load_session)
        self.load_btn.pack(fill=tk.X, pady=(10, 0))

        # Drivers: available -> selected
        ttk.Label(controls, text='Drivers').pack(anchor='w', pady=(12, 0))
        lists = ttk.Frame(controls)
        lists.pack(fill=tk.X)

        avail_frame = ttk.Frame(lists)
        avail_frame.pack(side=tk.LEFT)
        ttk.Label(avail_frame, text='Available').pack()
        self.available_lb = tk.Listbox(avail_frame, height=10, width=10,
                                       selectmode=tk.EXTENDED, exportselection=False)
        self.available_lb.pack()

        btns = ttk.Frame(lists)
        btns.pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text='▶', width=3, command=self._add_drivers).pack(pady=2)
        ttk.Button(btns, text='◀', width=3, command=self._remove_drivers).pack(pady=2)

        sel_frame = ttk.Frame(lists)
        sel_frame.pack(side=tk.LEFT)
        ttk.Label(sel_frame, text='Selected').pack()
        self.selected_lb = tk.Listbox(sel_frame, height=10, width=10,
                                      selectmode=tk.EXTENDED, exportselection=False)
        self.selected_lb.pack()

        # Plot type
        ttk.Label(controls, text='Plot').pack(anchor='w', pady=(12, 0))
        self.plot_var = tk.StringVar(value=list(plots.PLOTS)[0])
        ttk.Combobox(controls, textvariable=self.plot_var, state='readonly',
                     width=24, values=list(plots.PLOTS)).pack(fill=tk.X)

        self.plot_btn = ttk.Button(controls, text='Build plot',
                                   command=self._build_plot)
        self.plot_btn.pack(fill=tk.X, pady=(10, 0))

        # Status
        self.status_var = tk.StringVar(value='Pick a season, Grand Prix and session')
        ttk.Label(controls, textvariable=self.status_var, wraplength=200,
                  foreground='gray').pack(anchor='w', pady=(12, 0))

        # Plot area
        self.plot_frame = ttk.Frame(self.root, padding=10)
        self.plot_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        # Fill the Grand Prix list for the newest season
        if self.year_cb['values']:
            self.year_cb.current(0)
            self._on_year_change()

    # ---------- handlers ----------
    def _on_year_change(self, event=None):
        year = self.year_var.get()
        if not year:
            return
        self.status_var.set('Loading schedule…')
        self.gp_cb['values'] = []

        def work():
            try:
                events = f1_data.get_events(int(year))
            except Exception as exc:
                self.root.after(0, lambda: self.status_var.set(f'Schedule error: {exc}'))
                return

            def done():
                self.gp_cb['values'] = events
                if events:
                    self.gp_cb.current(0)
                self.status_var.set('Ready to load a session')
            self.root.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def _load_session(self):
        year, gp, stype = self.year_var.get(), self.gp_var.get(), self.session_var.get()
        if not (year and gp and stype):
            messagebox.showwarning('F1 Analytics', 'Pick a season, Grand Prix and session')
            return

        self.load_btn.config(state='disabled')
        self.status_var.set(f'Loading {gp} {year} ({stype})… the first load takes a few seconds')

        def work():
            try:
                session = f1_data.get_session(int(year), gp, stype)
                drivers = f1_data.get_drivers(session)
            except Exception as exc:
                self.root.after(0, lambda: self._load_failed(exc))
                return
            self.root.after(0, lambda: self._load_done(session, drivers))

        threading.Thread(target=work, daemon=True).start()

    def _load_failed(self, exc):
        self.load_btn.config(state='normal')
        self.status_var.set(f'Load error: {exc}')
        messagebox.showerror('F1 Analytics', f'Could not load the session:\n{exc}')

    def _load_done(self, session, drivers):
        self.session = session
        self.available_lb.delete(0, tk.END)
        self.selected_lb.delete(0, tk.END)
        for d in drivers:
            self.available_lb.insert(tk.END, d)
        self.load_btn.config(state='normal')
        self.status_var.set(f'Session loaded: {len(drivers)} drivers. '
                            f'Add drivers and build a plot.')

    def _add_drivers(self):
        existing = set(self.selected_lb.get(0, tk.END))
        for i in self.available_lb.curselection():
            code = self.available_lb.get(i)
            if code not in existing:
                self.selected_lb.insert(tk.END, code)

    def _remove_drivers(self):
        for i in reversed(self.selected_lb.curselection()):
            self.selected_lb.delete(i)

    def _build_plot(self):
        if self.session is None:
            messagebox.showwarning('F1 Analytics', 'Load a session first')
            return
        drivers = list(self.selected_lb.get(0, tk.END))
        if not drivers:
            messagebox.showwarning('F1 Analytics', 'Add at least one driver')
            return

        plot_fn = plots.PLOTS[self.plot_var.get()]
        self.status_var.set('Building plot…')
        try:
            fig = plot_fn(self.session, drivers)
        except Exception as exc:
            self.status_var.set(f'Plot error: {exc}')
            messagebox.showerror('F1 Analytics', f'Could not build the plot:\n{exc}')
            return

        self._show_figure(fig)
        self.status_var.set('Done')

    def _show_figure(self, fig):
        # Remove the previous plot
        if self.toolbar is not None:
            self.toolbar.destroy()
        if self.canvas is not None:
            self.canvas.get_tk_widget().destroy()

        self.canvas = FigureCanvasTkAgg(fig, master=self.plot_frame)
        self.canvas.draw()
        self.toolbar = NavigationToolbar2Tk(self.canvas, self.plot_frame)
        self.toolbar.update()
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)


if __name__ == '__main__':
    root = tk.Tk()
    F1App(root)
    root.mainloop()
