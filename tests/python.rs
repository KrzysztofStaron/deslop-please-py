//! End-to-end tests of `deslop-please --python` on the small projects in
//! `tests/python`. They need only `python3`.

use std::path::Path;
use std::process::Command;

const PROGRAM: &str = env!("CARGO_BIN_EXE_deslop-please");

/// The exit code and the output of a run in `tests/python/<project>`.
fn run(project: &str) -> (Option<i32>, String) {
    let directory = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/python")
        .join(project);
    let output = Command::new(PROGRAM)
        .arg("--python")
        .current_dir(directory)
        .output()
        .unwrap();
    let text = String::from_utf8(output.stdout).unwrap();
    (output.status.code(), text)
}

fn flags(project: &str, expected: &[&str]) {
    let (code, text) = run(project);
    assert_eq!(code, Some(1), "{text}");
    for line in expected {
        assert!(text.contains(line), "missing {line:?} in\n{text}");
    }
}

#[test]
fn a_clean_project_passes() {
    let (code, text) = run("clean");
    assert_eq!(code, Some(0), "{text}");
}

#[test]
fn code_that_works_on_the_fields_of_a_class_it_is_not_part_of_is_a_field_leak() {
    flags(
        "field_leak",
        &[
            "error[field-leak]: `RunQueue.items` is known outside `RunQueue`: 3 places in 2 modules",
            "error[field-leak]: `RunQueue.head` is known outside `RunQueue`: 1 place in 1 module",
            "kept: limit",
        ],
    );
}

#[test]
fn two_modules_that_use_each_other_are_a_module_cycle() {
    flags(
        "module_cycle",
        &["error[module-cycle]: 2 modules in `pkg` use each other in a circle"],
    );
}

#[test]
fn two_classes_that_know_each_other_are_a_tangle() {
    flags(
        "tangle",
        &["members: pkg.kernel.Tasks, pkg.kernel.timers.Timers"],
    );
}

#[test]
fn a_module_that_shows_most_of_its_functions_is_shallow() {
    flags(
        "shallow",
        &["error[shallow-module]: `pkg.store` is shallow: it shows 10 of its 11 functions"],
    );
}

#[test]
fn types_follow_loops_mappings_isinstance_and_comprehensions() {
    flags(
        "inferred",
        &[
            "error[field-leak]: `Ring.items` is known outside `Ring`: 2 places in 1 module",
            "error[field-leak]: `Ring.head` is known outside `Ring`: 1 place in 1 module",
        ],
    );
}
