"""
Plots for life cycle assessments interpretation
"""
from pyvis.network import Network
import os
from IPython.display import display, HTML, IFrame
from lca_algebraic.activity import Activity
from lcav.helpers import list_processes

USER_DB = 'Foreground DB'
default_process_tree_filename = 'process_tree.html'


def process_tree(model: Activity,
                 foreground_only: bool = True,
                 outfile: str = default_process_tree_filename):
    """
    Plots an interactive tree to visualize the activities and exchanges declared in the LCA module.
    """

    # Init network
    net = Network(notebook=True, directed=True, layout=True, cdn_resources='remote')

    # Get processes hierarchy
    df = list_processes(model, foreground_only)
    activities = df['activity']
    df['description'] = df['activity'] + '\n' + df['unit'].fillna('')
    descriptions = df['description']
    parents = df['parent']
    amounts = df['exchange']
    levels = df['level']
    dbs = df['database']

    # Populate network
    edge_data = zip(activities, descriptions, parents, amounts, levels, dbs)

    for e in edge_data:
        src = e[0]
        desc = e[1]
        dst = e[2]
        w = e[3]
        n = e[4]
        db = e[5]

        color = '#97c2fc' if db == USER_DB else 'lightgrey'
        if dst == "":
            net.add_node(src, desc, title=src, level=n + 1, shape='box', color=color)
            continue
        net.add_node(src, desc, title=src, level=n + 1, shape='box', color=color)
        net.add_node(dst, dst, title=dst, level=n, shape='box')
        net.add_edge(src, dst, label=str(w))

    # Options
    net.set_edge_smooth('vertical')
    net.toggle_physics(False)

    # Save
    net.save_graph(outfile)

    # Display in Jupyter Notebook
    outfile = os.path.relpath(outfile)
    display(IFrame(src=outfile, width="100%", height=700))

