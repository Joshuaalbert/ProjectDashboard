import networkx as nx
from jax import numpy as jnp, vmap, random, jit, tree_map
import numpy as np

def _compute_event_probabilities(G,num_samples):
    topo = list(nx.topological_sort(G))
    def _single_run(key):
        for n in topo:
            key, _key = random.split(key, 2)
            pred = jnp.all(jnp.asarray([G.nodes[j]['success'] for j in G.predecessors(n)]))
            cond_success = random.uniform(_key) < G.nodes[n]['success_prob']/100.
            success = pred & cond_success
            G.nodes[n]['success'] = success
            G.nodes[n]['start_prob'] = pred

        results = {n:(G.nodes[n]['success'], G.nodes[n]['start_prob']) for n in G.nodes}
        return results

    out = vmap(_single_run)(random.split(random.PRNGKey(42),num_samples))
    probs = tree_map(lambda x: jnp.mean(x,axis=0), out)
    for key in probs:
        G.nodes[key]['success'] = float(probs[key][0])
        G.nodes[key]['start_prob'] = float(probs[key][1])

def prod(l):
    if len(l) == 0:
        return 1.
    return np.prod(l)

def compute_event_probabilities(G):
    for n in nx.topological_sort(G):
        G.nodes[n]['start_prob'] = prod([G.nodes[j]['success'] for j in G.predecessors(n)])
        G.nodes[n]['success'] = G.nodes[n]['success_prob']/100. * G.nodes[n]['start_prob']
