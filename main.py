
import f1_data
import plots

YEAR = 2024
GP = 'Monza'
SESSION = 'R'  
DRIVERS = ['VER', 'LEC']


def main():
    f1_data.enable_cache()
    session = f1_data.get_session(YEAR, GP, SESSION)

    print(session.results[['DriverNumber', 'Abbreviation', 'TeamName', 'Position']])

    for label, plot_fn in plots.PLOTS.items():
        fig = plot_fn(session, DRIVERS)
        filename = f'{label}.png'.replace(' ', '_')
        fig.savefig(filename, dpi=120, bbox_inches='tight')
        print(f'Сохранено: {filename}')


if __name__ == '__main__':
    main()
