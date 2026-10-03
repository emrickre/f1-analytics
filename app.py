"""Tkinter-интерфейс для анализа Формулы-1 через FastF1.

Запуск:  python app.py
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

        self.session = None          # текущая загруженная сессия
        self.canvas = None           # FigureCanvasTkAgg
        self.toolbar = None

        f1_data.enable_cache()
        self._build_ui()

    # ---------- построение интерфейса ----------
    def _build_ui(self):
        controls = ttk.Frame(self.root, padding=10)
        controls.pack(side=tk.LEFT, fill=tk.Y)

        # Год
        ttk.Label(controls, text='Год').pack(anchor='w')
        self.year_var = tk.StringVar()
        self.year_cb = ttk.Combobox(controls, textvariable=self.year_var,
                                    state='readonly', width=24,
                                    values=[str(y) for y in f1_data.get_years()])
        self.year_cb.pack(fill=tk.X)
        self.year_cb.bind('<<ComboboxSelected>>', self._on_year_change)

        # Гран-при
        ttk.Label(controls, text='Гран-при').pack(anchor='w', pady=(8, 0))
        self.gp_var = tk.StringVar()
        self.gp_cb = ttk.Combobox(controls, textvariable=self.gp_var,
                                  state='readonly', width=24)
        self.gp_cb.pack(fill=tk.X)

        # Сессия
        ttk.Label(controls, text='Сессия').pack(anchor='w', pady=(8, 0))
        self.session_var = tk.StringVar(value='R')
        ttk.Combobox(controls, textvariable=self.session_var, state='readonly',
                     width=24, values=f1_data.SESSION_TYPES).pack(fill=tk.X)

        self.load_btn = ttk.Button(controls, text='Загрузить сессию',
                                   command=self._load_session)
        self.load_btn.pack(fill=tk.X, pady=(10, 0))

        # Пилоты: доступные -> выбранные
        ttk.Label(controls, text='Пилоты').pack(anchor='w', pady=(12, 0))
        lists = ttk.Frame(controls)
        lists.pack(fill=tk.X)

        avail_frame = ttk.Frame(lists)
        avail_frame.pack(side=tk.LEFT)
        ttk.Label(avail_frame, text='Доступные').pack()
        self.available_lb = tk.Listbox(avail_frame, height=10, width=10,
                                       selectmode=tk.EXTENDED, exportselection=False)
        self.available_lb.pack()

        btns = ttk.Frame(lists)
        btns.pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text='▶', width=3, command=self._add_drivers).pack(pady=2)
        ttk.Button(btns, text='◀', width=3, command=self._remove_drivers).pack(pady=2)

        sel_frame = ttk.Frame(lists)
        sel_frame.pack(side=tk.LEFT)
        ttk.Label(sel_frame, text='Выбранные').pack()
        self.selected_lb = tk.Listbox(sel_frame, height=10, width=10,
                                      selectmode=tk.EXTENDED, exportselection=False)
        self.selected_lb.pack()

        # Тип графика
        ttk.Label(controls, text='Тип графика').pack(anchor='w', pady=(12, 0))
        self.plot_var = tk.StringVar(value=list(plots.PLOTS)[0])
        ttk.Combobox(controls, textvariable=self.plot_var, state='readonly',
                     width=24, values=list(plots.PLOTS)).pack(fill=tk.X)

        self.plot_btn = ttk.Button(controls, text='Построить график',
                                   command=self._build_plot)
        self.plot_btn.pack(fill=tk.X, pady=(10, 0))

        # Статус
        self.status_var = tk.StringVar(value='Выберите год, Гран-при и сессию')
        ttk.Label(controls, textvariable=self.status_var, wraplength=200,
                  foreground='gray').pack(anchor='w', pady=(12, 0))

        # Область графика
        self.plot_frame = ttk.Frame(self.root, padding=10)
        self.plot_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        # Предзаполнить список Гран-при для первого года
        if self.year_cb['values']:
            self.year_cb.current(0)
            self._on_year_change()

    # ---------- обработчики ----------
    def _on_year_change(self, event=None):
        year = self.year_var.get()
        if not year:
            return
        self.status_var.set('Загрузка расписания…')
        self.gp_cb['values'] = []

        def work():
            try:
                events = f1_data.get_events(int(year))
            except Exception as exc:
                self.root.after(0, lambda: self.status_var.set(f'Ошибка расписания: {exc}'))
                return

            def done():
                self.gp_cb['values'] = events
                if events:
                    self.gp_cb.current(0)
                self.status_var.set('Готово к загрузке сессии')
            self.root.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def _load_session(self):
        year, gp, stype = self.year_var.get(), self.gp_var.get(), self.session_var.get()
        if not (year and gp and stype):
            messagebox.showwarning('F1 Analytics', 'Выберите год, Гран-при и сессию')
            return

        self.load_btn.config(state='disabled')
        self.status_var.set(f'Загрузка {gp} {year} ({stype})… это может занять время')

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
        self.status_var.set(f'Ошибка загрузки: {exc}')
        messagebox.showerror('F1 Analytics', f'Не удалось загрузить сессию:\n{exc}')

    def _load_done(self, session, drivers):
        self.session = session
        self.available_lb.delete(0, tk.END)
        self.selected_lb.delete(0, tk.END)
        for d in drivers:
            self.available_lb.insert(tk.END, d)
        self.load_btn.config(state='normal')
        self.status_var.set(f'Сессия загружена: {len(drivers)} пилотов. '
                            f'Добавьте пилотов и постройте график.')

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
            messagebox.showwarning('F1 Analytics', 'Сначала загрузите сессию')
            return
        drivers = list(self.selected_lb.get(0, tk.END))
        if not drivers:
            messagebox.showwarning('F1 Analytics', 'Добавьте хотя бы одного пилота')
            return

        plot_fn = plots.PLOTS[self.plot_var.get()]
        self.status_var.set('Построение графика…')
        try:
            fig = plot_fn(self.session, drivers)
        except Exception as exc:
            self.status_var.set(f'Ошибка построения: {exc}')
            messagebox.showerror('F1 Analytics', f'Ошибка построения графика:\n{exc}')
            return

        self._show_figure(fig)
        self.status_var.set('Готово')

    def _show_figure(self, fig):
        # Убрать предыдущий график
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
