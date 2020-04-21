# Copyright 2020 Stanford University

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import itertools, random, json, time, sys, argparse
from collections import defaultdict
import z3

from .interpret import interpret, FOLFile
from .parse import parse
from .logic import Signature, Environment, Model, And, Or, Not, Exists, Forall, Equal, Relation, Formula, Term, Var, Func
from .check import check
from .separate import Separator, SeparatorNaive, SeparatorReductionV1, SeparatorReductionV2, GeneralizedSeparator, HybridSeparator
from .timer import Timer, UnlimitedTimer, TimeoutException
from .cvc4 import solve_with_cvc4
from typing import *

sorts_to_z3: Dict[str, z3.SortRef] = {}
z3_rel_func: Dict[str, z3.FuncDeclRef] = {}

def toZ3(f: Union[Formula, Term], env: Environment) -> z3.ExprRef:
    def R(f: Union[Formula, Term]) -> z3.ExprRef: return toZ3(f, env)
    if isinstance(f, And):
        if len(f.c) == 0:
            return z3.BoolVal(True)
        return z3.And(*[R(x) for x in f.c])
    elif isinstance(f, Or):
        if len(f.c) == 0:
            return z3.BoolVal(False)
        return z3.Or(*[R(x) for x in f.c])
    elif isinstance(f, Not):
        return z3.Not(R(f.f))
    elif isinstance(f, Relation):
        return z3_rel_func[f.rel](*[R(x) for x in f.args])
    elif isinstance(f, Func):
        return z3_rel_func[f.f](*[R(x) for x in f.args])
    elif isinstance(f, Equal):
        return R(f.args[0]) == R(f.args[1])
    elif isinstance(f, Var):
        v = env.lookup_var(f.var)
        if v is None: raise RuntimeError("Cannot convert invalid formula to z3")
        return z3.Const(f.var, sorts_to_z3[v])
    elif isinstance(f, Forall) or isinstance(f, Exists):
        env.bind(f.var, f.sort)
        sub_f = toZ3(f.f, env)
        env.pop()
        bv = z3.Const(f.var, sorts_to_z3[f.sort])
        return z3.ForAll(bv, sub_f) if isinstance(f, Forall) else z3.Exists(bv, sub_f)
    else:
        print ("Can't translate", f)
        assert False

def extract_model(m: z3.ModelRef, sig: Signature, label: str = "") -> Model:
    M = Model(sig)
    M.label = label
    z3_to_model_elems = {}
    # add elements
    for sort in sorted(sig.sorts):
        univ = m.get_universe(sorts_to_z3[sort])
        assert len(univ) > 0
        for e in sorted(univ, key = str):
            z3_to_model_elems[str(e)] = M.add_elem(str(e), sort)
    # assign constants
    for const, sort in sorted(sig.constants.items()):
        M.add_constant(const, str(m.eval(z3.Const(const, sorts_to_z3[sort]), model_completion=True)))
    # assign relations
    for rel, sorts in sorted(sig.relations.items()):
        univs = [m.get_universe(sorts_to_z3[s]) for s in sorts]
        for t in itertools.product(*univs):
            ev = m.eval(z3_rel_func[rel](*t), model_completion = True)
            if ev:
                M.add_relation(rel, [str(x) for x in t])
    for func, (sorts, _) in sorted(sig.functions.items()):
        univs = [m.get_universe(sorts_to_z3[s]) for s in sorts]
        for t in itertools.product(*univs):
            ev = m.eval(z3_rel_func[func](*t), model_completion = True)
            M.add_function(func, [str(x) for x in t], str(ev))
    return M

def fm(a: Formula, b: Formula, env: Environment, solver: z3.Solver, timer: Timer) -> Tuple[z3.CheckSatResult, Optional[z3.ModelRef]]:
    solver.push()
    solver.add(toZ3(a, env))
    solver.add(z3.Not(toZ3(b, env)))
    r = timer.solver_check(solver)
    m = solver.model() if r == z3.sat else None
    solver.pop()
    return (r, m)

def bound_sort_counts(solver: z3.Solver, bounds: Dict[str, int]) -> None:
    for sort, K in bounds.items():
        S = sorts_to_z3[sort]
        bv = z3.Const("elem_{}".format(sort), S)
        solver.add(z3.ForAll(bv, z3.Or(*[z3.Const("elem_{}_{}".format(sort, i), S) == bv for i in range(K)])))            

def find_model_or_equivalence(current: Formula, formula: Formula, env: Environment, s: z3.Solver, t: Timer) -> Optional[Model]:
    (r1, m) = fm(current, formula, env, s, t)
    if m is not None:
        for k in range(1, 100000):
            s.push()
            bound_sort_counts(s, dict((s,k) for s in env.sig.sorts))
            (_, m) = fm(current, formula, env, s, t)
            s.pop()
            if m is not None:
                return extract_model(m, env.sig, "-")
        assert False
    (r2, m) = fm(formula, current, env, s, t)
    if m is not None:
        for k in range(1, 100000):
            s.push()
            bound_sort_counts(s, dict((s,k) for s in env.sig.sorts))
            (_, m) = fm(formula, current, env, s, t)
            s.pop()
            if m is not None:
                return extract_model(m, env.sig, "+")
        assert False
    if r1 == z3.unsat and r2 == z3.unsat:
        return None
    
    # TODO: try bounded model checking up to some limit
    # test = Timer(1000000)
    # with test:
    #     r = fm(current, formula, env, s, test)
    #     if repr(r) != "unknown":
    #         print("Inconsistency in timeouts")

    raise RuntimeError("Z3 did not produce equivalence or model")

def find_model_or_equivalence_cvc4(current: Formula, formula: Formula, env: Environment, s: z3.Solver, t: Timer) -> Optional[Model]:

    # Check current => formula
    s.push()
    s.add(toZ3(current, env))
    s.add(z3.Not(toZ3(formula, env)))
    (r1, m) = solve_with_cvc4(s, env.sig, timeout=t.remaining())
    s.pop()
    if m is not None:
        m.label = '-'
        return m
    
    # Check formula => current
    s.push()
    s.add(toZ3(formula, env))
    s.add(z3.Not(toZ3(current, env)))
    (r2, m) = solve_with_cvc4(s, env.sig, timeout=t.remaining())
    s.pop()
    if m is not None:
        m.label = '+'
        return m

    if r1 == z3.unsat and r2 == z3.unsat:
        return None
    raise RuntimeError("CVC4 did not produce equivalence or model")



class LearningResult(object):
    def __init__(self, success: bool, current: Formula, ct: Timer, st: Timer, mt: Timer):
        self.success = success
        self.current = current
        self.counterexample_timer = ct
        self.separation_timer = st
        self.matrix_timer = mt
        self.models: List[Model] = []
        self.reason = ""


def learn(sig: Signature, axioms: List[Formula], formula: Formula, timeout: float, args: Any) -> LearningResult:
    result = LearningResult(False, Or([]), Timer(timeout), Timer(timeout), UnlimitedTimer())
    
    S = SeparatorReductionV1 if args.separator == 'v1' else\
        SeparatorReductionV2 if args.separator == 'v2' else\
        GeneralizedSeparator if args.separator == 'generalized' else\
        HybridSeparator if args.separator == 'hybrid' else\
        SeparatorNaive 
        
    separator: Separator = S(sig, quiet=args.quiet, logic=args.logic, epr_wrt_formulas=axioms+[formula, Not(formula)]) 

    env = Environment(sig)
    s = z3.Solver()
    for sort in sig.sorts:
        sorts_to_z3[sort] = z3.DeclareSort(sort)
    for const, sort in sig.constants.items():
        z3.Const(const, sorts_to_z3[sort])
    for rel, sorts in sig.relations.items():
        z3_rel_func[rel] = z3.Function(rel, *[sorts_to_z3[x] for x in sorts], z3.BoolSort())
    for fun, (sorts, ret) in sig.functions.items():
        z3_rel_func[fun] = z3.Function(fun, *[sorts_to_z3[x] for x in sorts], sorts_to_z3[ret])

    for ax in axioms:
        s.add(toZ3(ax, env))

    p_constraints: List[int] = []
    n_constraints: List[int] = []
     
    try:
        while True:
            with result.counterexample_timer:
                if not args.quiet:
                    print ("Checking formula")
                if not args.no_cvc4:
                    r = find_model_or_equivalence_cvc4(result.current, formula, env, s, result.counterexample_timer)
                else:
                    r = find_model_or_equivalence(result.current, formula, env, s, result.counterexample_timer)
                
                result.counterexample_timer.check_time()
                if r is None:
                    if not args.quiet:
                        print ("formula matches!")
                        print (result.current)
                        # f = open("/tmp/out.fol", "w")
                        # f.write(str(sig))
                        # for m in result.models:
                        #     f.write(str(m))
                        # f.close()
                    result.success = True
                    return result
            
            with result.separation_timer:
                ident = separator.add_model(r)
                result.models.append(r)
                if r.label.startswith("+"):
                    p_constraints.append(ident)
                else:
                    n_constraints.append(ident)

                if not args.quiet:
                    print ("New model is:")
                    print (r)
                    print ("Have new model, now have", len(result.models), "models total")
                if True:
                    c = separator.separate(pos=p_constraints, neg=n_constraints, imp=[], max_clauses = args.max_clauses, max_depth= args.max_depth, timer = result.separation_timer, matrix_timer = result.matrix_timer)
                if c is None:
                    result.reason = "couldn't separate models under given restrictions"
                    break
                if not args.quiet:
                    print("Learned new possible formula: ", c)
                result.current = c
    except TimeoutException:
        result.reason = "timeout"
    except RuntimeError as e:
        print("Error:", e)
        #raise e
        result.reason = str(e)

    return result


def separate(f: FOLFile, timeout: float, args: Any) -> LearningResult:
    result = LearningResult(False, Or([]), Timer(timeout), Timer(timeout), UnlimitedTimer())
    
    S = SeparatorReductionV1 if args.separator == 'v1' else\
        SeparatorReductionV2 if args.separator == 'v2' else\
        GeneralizedSeparator if args.separator == 'generalized' else\
        HybridSeparator if args.separator == 'hybrid' else\
        SeparatorNaive 
        
    separator: Separator = S(f.sig, quiet=args.quiet, logic=args.logic, epr_wrt_formulas=f.axioms)

    result.models = f.models
    mapping: DefaultDict[str, List[int]] = defaultdict(list)
    for m in f.models:
        mapping[m.label].append(separator.add_model(m))

    try:
        with result.separation_timer:
            p_constraints = [x for a in f.constraint_pos for x in mapping[a]]
            n_constraints = [x for a in f.constraint_neg for x in mapping[a]]
            i_constraints = [(x, y) for a, b in f.constraint_imp for x in mapping[a] for y in mapping[b]]
            print(p_constraints, n_constraints, i_constraints)
            c = separator.separate(pos=p_constraints, neg=n_constraints, imp=i_constraints, \
                                   max_clauses = args.max_clauses, max_depth= args.max_depth, \
                                   timer = result.separation_timer, matrix_timer = result.matrix_timer)
            if c is None:
                result.reason = "couldn't separate models under given restrictions"
            else:
                result.current = c
                result.success = True
                for m in f.models:
                    print(m.label, check(c, m))
    except TimeoutException:
        result.reason = "timeout"
    
    return result
    