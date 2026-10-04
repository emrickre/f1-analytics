"""Save all explorer plots for one session as PNG files.

    python main.py                                   # 2024 Monza race, VER vs LEC
    python main.py 2024 "Las Vegas" R NOR PIA
    python main.py 2026 Monaco Q LEC HAM --out figures/monaco
"""

import argparse
from pathlib import Path

import f1_data
import plots


def main():
    p = argparse.ArgumentParser(description='Save F1 session plots (data: OpenF1)')
    p.add_argument('year', type=int, nargs='?', default=2024)
    p.add_argument('gp', nargs='?', default='Monza', help='Grand Prix name or location')
    p.add_argument('session', nargs='?', default='R', choices=f1_data.SESSION_TYPES)
    p.add_argument('drivers', nargs='*', default=['VER', 'LEC'])
    p.add_argument('--out', default='figures', help='output folder')
    a = p.parse_args()

    print(f'Loading {a.year} {a.gp} {a.session} from OpenF1 (cached after the first run)…')
    session = f1_data.get_session(a.year, a.gp, a.session)
    order = f1_data.get_drivers(session)
    print(f'{session.title} {session.name}: {len(session.laps)} laps, '
          f'order: {" ".join(order[:10])}')

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for label, plot_fn in plots.PLOTS.items():
        fig = plot_fn(session, a.drivers)
        path = out / f'{label.lower().replace(" ", "_")}.png'
        fig.savefig(path, dpi=120, bbox_inches='tight')
        print(f'saved {path}')


if __name__ == '__main__':
    main()
