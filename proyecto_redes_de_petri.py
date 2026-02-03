import collections
import graphviz
import os
import re

def parse_input(filepath):
    """
    Parses the input file.
    Expected format:
    NTR: 3
    TR1: A B C ...
    Or just lines of symbols.
    """
    traces = {}
    with open(filepath, 'r') as f:
        lines = f.readlines()

    trace_counter = 1
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("NTR"):
            continue
        
        # Remove label like "TR1:" if present
        if ":" in line:
            parts = line.split(":", 1)
            # label = parts[0].strip() # Not strictly needed if we just number them
            content = parts[1].strip()
        else:
            content = line
            
        events = content.split()
        if events:
            trace_id = f"TR{trace_counter}"
            traces[trace_id] = events
            trace_counter += 1
            
    return traces

def compute_bef(traces):
    """
    Computes BEF(lambda).
    Returns a set of tuples: ((u, v), trace_id)
    where u, v are events or '-'
    """
    bef = set()
    
    for tid, events in traces.items():
        if not events:
            continue
            
        # First element
        bef.add( (('-', events[0]), tid) )
        
        # Pairs
        for i in range(len(events) - 1):
            u = events[i]
            v = events[i+1]
            bef.add( ((u, v), tid) )
            
        # Last element
        bef.add( ((events[-1], '-'), tid) )
        
    return bef

def compute_conc(bef, traces):
    """
    Computes Conc set.
    Conc = { ((a,b), si), ((b,a), sj) in BEF | si != sj }
    Actually, Conc is the subset of BEF elements that are involved in a conflict.
    If (a,b) in si and (b,a) in sj:
       Add ((a,b), si) to Conc
       Add ((b,a), sj) to Conc
    """
    conc = set()
    
    # We need to find pairs (a,b) where (b,a) also exists in some other trace
    # Let's map (u,v) -> list of trace_ids
    pair_occurrences = collections.defaultdict(list)
    for (u, v), tid in bef:
        if u == '-' or v == '-':
            continue
        pair_occurrences[(u, v)].append(tid)
        
    # Now check for conflicts
    # Iterate over all unique pairs found
    all_pairs = list(pair_occurrences.keys())
    
    for (a, b) in all_pairs:
        # If the reverse pair exists in ANY trace
        if (b, a) in pair_occurrences:
             # Then ALL occurrences of (a,b) are part of Conc (if traces differ?)
             # The definition: Conc = { ((a,b), si), ((b,a), sj) in BEF | si != sj }
             # It implies we need at least one case of swap.
             # If (a,b) in T1, (b,a) in T1? That would be a cycle in one trace, possible but let's stick to definition.
             # "si != sj".
             # Check if there exists a trace with (b,a) different from the traces with (a,b)
             
             traces_ab = set(pair_occurrences[(a, b)])
             traces_ba = set(pair_occurrences[(b, a)])
             
             # If there is any si in traces_ab and sj in traces_ba such that si != sj
             # If the sets are disjoint, trivial. 
             # If they overlap, (a,b) and (b,a) in same trace?
             
             conflict_found = False
             for si in traces_ab:
                 for sj in traces_ba:
                     if si != sj:
                         conflict_found = True
                         break
                 if conflict_found:
                     break
             
             if conflict_found:
                 # Add all relevant entries to Conc?
                 # Def: Conc contains elements from BEF.
                 # Usually defined as: The set of pairs that are unordered globally.
                 # The subset of BEF edges that contradict others.
                 # "Conc = { ((a,b), si) ... }" includes specific instances.
                 # Technically checking if THAT SPECIFIC instance conflicts with another actual instance.
                 
                 # Let's collect all such pairs.
                 pass

    # Re-iterating strictly by definition
    # Brute force (dataset is small)
    bef_list = list(bef)
    for i in range(len(bef_list)):
        item1 = bef_list[i] # ((a,b), si)
        pair1 = item1[0]
        si = item1[1]
        a, b = pair1
        
        if a == '-' or b == '-':
            continue
            
        for j in range(len(bef_list)):
            item2 = bef_list[j] # ((x,y), sj)
            pair2 = item2[0]
            sj = item2[1]
            x, y = pair2
            
            if x == '-' or y == '-':
                continue
                
            # Check condition: pair2 is reverse of pair1 AND sj != si
            if x == b and y == a and si != sj:
                conc.add(item1)
                # item2 will be added when we iterate to it or by symmetry here? 
                # Better let the outer loop find it to be safe, or add both.
                conc.add(item2)
                
    return conc

def build_graph(relations, title, filename):
    """
    Builds a directed graph from relations using Graphviz.
    """
    dot = graphviz.Digraph(comment=title)
    dot.attr(label=title, labelloc='t')
    
    # Extract unique edges between events (ignoring '-' for now as per instructions usually implying Event Graph)
    edges = set()
    node_set = set()
    for (u, v), tid in relations:
        if u != '-' and v != '-':
            edges.add((u, v))
            node_set.add(u)
            node_set.add(v)
            
    for n in node_set:
        dot.node(n, shape='circle', style='filled', fillcolor='lightblue')
            
    for u, v in edges:
        dot.edge(u, v)

    output_path = os.path.splitext(filename)[0]
    try:
        dot.render(output_path, format='png', cleanup=True)
        print(f"Graph saved to {filename}")
    except Exception as e:
        print(f"Error saving {filename}: {e}")

def build_nfa_trivial(traces, filename):
    dot = graphviz.Digraph(comment='NFA Trivial')
    dot.attr(rankdir='LR', label='NFA Trivial', labelloc='t')
    
    # States
    initial_state = "q_init"
    final_state = "q_final"
    dot.node(initial_state, shape='circle', style='filled', fillcolor='lightgrey')
    dot.node(final_state, shape='doublecircle', style='filled', fillcolor='lightgrey')
    
    state_counter = 0
    
    for tid, events in traces.items():
        curr = initial_state
        for i, evt in enumerate(events):
            if i == len(events) - 1:
                target = final_state
            else:
                target = f"{tid}_{state_counter}"
                state_counter += 1
                dot.node(target, shape='circle')
            
            dot.edge(curr, target, label=evt)
            curr = target

    output_path = os.path.splitext(filename)[0]
    try:
        dot.render(output_path, format='png', cleanup=True)
        print(f"Graph saved to {filename}")
    except Exception as e:
        print(f"Error saving {filename}: {e}")

def build_nfa_compact_prefix(traces, filename):
    dot = graphviz.Digraph(comment='NFA Compact')
    dot.attr(rankdir='LR', label='NFA Compact (Prefix Merged)', labelloc='t')
    
    root = "root"
    final_state = "final"
    dot.node(root, shape='circle', style='filled', fillcolor='lightgrey')
    dot.node(final_state, shape='doublecircle', style='filled', fillcolor='lightgrey')
    
    node_counter = 0
    state_map = {} # path_tuple -> node_id
    added_edges = set()
    
    for tid, events in traces.items():
        curr = root
        path = ()
        for i, evt in enumerate(events):
            is_last = (i == len(events) - 1)
            
            if is_last:
                # Add edge to final
                if (curr, final_state, evt) not in added_edges:
                    dot.edge(curr, final_state, label=evt)
                    added_edges.add((curr, final_state, evt))
            else:
                # Intermediate
                path = path + (evt,)
                if path in state_map:
                    next_node = state_map[path]
                else:
                    next_node = f"s{node_counter}"
                    node_counter += 1
                    state_map[path] = next_node
                    dot.node(next_node, shape='circle')
                
                if (curr, next_node, evt) not in added_edges:
                    dot.edge(curr, next_node, label=evt)
                    added_edges.add((curr, next_node, evt))
                
                curr = next_node

    output_path = os.path.splitext(filename)[0]
    try:
        dot.render(output_path, format='png', cleanup=True)
        print(f"Graph saved to {filename}")
    except Exception as e:
        print(f"Error saving {filename}: {e}")

def main():
    input_file = "input.txt"
    if not os.path.exists(input_file):
        print(f"Error: {input_file} not found.")
        return

    traces = parse_input(input_file)
    print("Traces loaded:", traces)
    
    # 1. BEF
    bef = compute_bef(traces)
    print("\nBEF(lambda):")
    # Sort for consistent display
    sorted_bef = sorted(list(bef), key=lambda x: (x[1], x[0][0], x[0][1]))
    for pair in sorted_bef:
        print(pair)
        
    # 2. Graph of BEF
    build_graph(bef, "BEF Graph (G<)", "images/figura1_grafo_precedencia_BEF.png")
    
    # 3. Conc and PO
    conc = compute_conc(bef, traces)
    print("\nConc:")
    for c in sorted(list(conc), key=lambda x: (x[1], x[0][0])):
        print(c)
        
    po = bef - conc
    print("\nPO(lambda) = BEF \\ Conc:")
    for p in sorted(list(po), key=lambda x: (x[1], x[0][0])):
        print(p)
        
    # PO Graph
    build_graph(po, "PO Graph", "images/figura2_orden_parcial_PO.png")
    
    # 4. NFA Trivial
    build_nfa_trivial(traces, "images/figura4_automata_trivial.png")
    
    # 5. NFA Compact (Prefix)
    build_nfa_compact_prefix(traces, "images/figura5_automata_compacto.png")
    
    print("\nProcessing complete. Check generated PNG files in images/ folder.")

if __name__ == "__main__":
    main()
