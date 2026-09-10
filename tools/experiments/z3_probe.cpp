// Isolated Z3 registration A/B: semantic smoke coverage, not an arrangement corpus.
// Link this identical source with each registry; compare deterministic stdout.
#include <z3++.h>
#include <iostream>
#include <stdexcept>
#include <string>

namespace {
void require(bool condition, const std::string& message)
{
    if (!condition)
        throw std::runtime_error(message);
}

void verify(z3::solver& solver, const z3::expr_vector& assumptions,
            z3::check_result expected, const char* label)
{
    const z3::check_result result = solver.check(assumptions);
    require(result == expected, std::string(label) + ": unexpected solver result");
    if (result == z3::sat) {
        const z3::model model = solver.get_model();
        const z3::expr_vector assertions = solver.assertions();
        for (unsigned i = 0; i < assertions.size(); ++i)
            require(model.eval(assertions[i], true).is_true(), std::string(label) + ": invalid asserted constraint");
        for (unsigned i = 0; i < assumptions.size(); ++i)
            require(model.eval(assumptions[i], true).is_true(), std::string(label) + ": invalid assumption");
    }
    std::cout << label << ' ' << (expected == z3::sat ? "SAT" : "UNSAT") << " verified\n";
}
} // namespace

int main(int argc, char** argv)
{
    try {
        // Production sets a global timeout and constructs the default solver.
        z3::set_param("timeout", 5000);
        z3::context ctx;
        require(argc == 2, "pass present/absent/none for the expected tactic registration");
        const std::string expectation = argv[1];
        require(expectation == "present" || expectation == "absent" || expectation == "none", "invalid registry expectation");
        if (expectation == "none")
            require(Z3_get_num_tactics(ctx) == 0, "named tactic registry is not empty");
        bool found_testing_tactic = false;
        for (unsigned i = 0; i < Z3_get_num_tactics(ctx); ++i)
            if (std::string(Z3_get_tactic_name(ctx, i)) == "subpaving")
                found_testing_tactic = true;
        require(found_testing_tactic == (expectation == "present"), "testing-tactic registry did not change as expected");
        std::cout << "registry expectation verified\n";
        z3::expr_vector empty(ctx);
        {
            z3::solver solver(ctx);
            solver.set("timeout", 5000u);
            const z3::expr x = ctx.real_const("rational_x");
            const z3::expr y = ctx.real_const("rational_y");
            const z3::expr enabled = ctx.bool_const("enabled");
            solver.add(x == ctx.real_val("1/3"));
            solver.add(x + y == 1 && y > x);
            solver.add(enabled == (y == ctx.real_val("2/3")));
            verify(solver, empty, z3::sat, "rational_empty_assumptions");
            require(solver.get_model().eval(y == ctx.real_val("2/3")).is_true(), "exact rational model mismatch");

            z3::expr_vector assumptions(ctx);
            assumptions.push_back(enabled);
            assumptions.push_back(x < ctx.real_val("1/2"));
            verify(solver, assumptions, z3::sat, "nonempty_assumptions_sat");
            assumptions.push_back(x > ctx.real_val("1/2"));
            verify(solver, assumptions, z3::unsat, "nonempty_assumptions_unsat");
            verify(solver, empty, z3::sat, "assumptions_are_temporary");
            solver.push();
            solver.add(y < x);
            verify(solver, empty, z3::unsat, "incremental_push_unsat");
            solver.pop();
            verify(solver, empty, z3::sat, "incremental_pop_sat");
        }
        {
            z3::solver solver(ctx);
            const z3::expr x = ctx.real_const("inconsistent_x");
            solver.add(x > ctx.real_val("7/5") && x < ctx.real_val("6/5"));
            verify(solver, empty, z3::unsat, "fresh_empty_assumptions_unsat");
        }
        {
            z3::solver solver(ctx);
            solver.set("timeout", 5000u);
            const z3::expr x0 = ctx.real_const("x0"), y0 = ctx.real_const("y0");
            const z3::expr x1 = ctx.real_const("x1"), y1 = ctx.real_const("y1");
            const z3::expr t0 = ctx.real_const("t0"), t1 = ctx.real_const("t1");
            const z3::expr before = ctx.bool_const("first_before_second");
            const z3::expr width = ctx.real_val("3/2"), height = ctx.real_val(1);
            const z3::expr clearance = ctx.real_val("1/4");
            for (const z3::expr& x : {x0, x1})
                solver.add((x == 0 || x == 2 || x == 4) && x >= 0 && x + width <= 6);
            for (const z3::expr& y : {y0, y1})
                solver.add((y == 0 || y == 2) && y >= 0 && y + height <= 3);
            solver.add(x0 + width <= x1 || x1 + width <= x0 || y0 + height <= y1 || y1 + height <= y0);
            solver.add((t0 == 0 || t0 == 1) && (t1 == 0 || t1 == 1) && t0 != t1);
            solver.add(before == (t0 < t1));
            solver.add(z3::implies(before, x0 + width + clearance <= x1 || y0 + height + clearance <= y1));
            solver.add(z3::implies(!before, x1 + width + clearance <= x0 || y1 + height + clearance <= y0));
            z3::expr_vector assumptions(ctx);
            assumptions.push_back(before);
            const char* labels[] = {"layout_model_1", "layout_model_2", "layout_model_3"};
            for (const char* label : labels) {
                verify(solver, assumptions, z3::sat, label);
                const z3::model model = solver.get_model();
                z3::expr different = ctx.bool_val(false);
                for (const z3::expr& value : {x0, y0, x1, y1, t0, t1})
                    different = different || value != model.eval(value, true);
                solver.add(different); // Require another distinct feasible placement.
            }
            assumptions.resize(0);
            assumptions.push_back(!before);
            verify(solver, assumptions, z3::sat, "layout_reverse_order");
            solver.push();
            solver.add(x0 == x1 && y0 == y1);
            verify(solver, empty, z3::unsat, "layout_collision_unsat");
            solver.pop();
            verify(solver, empty, z3::sat, "layout_recovered");
        }
        std::cout << "Z3 registration semantic checks passed\n";
        return 0;
    } catch (const z3::exception& error) {
        std::cerr << "Z3 error: " << error.msg() << '\n';
    } catch (const std::exception& error) {
        std::cerr << "FAIL: " << error.what() << '\n';
    }
    return 1;
}
