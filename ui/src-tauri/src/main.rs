fn main() {
    if std::env::args_os().skip(1).eq(["--self-test"]) {
        if let Err(error) = nebula_ui_lib::self_test() {
            eprintln!("Nebula desktop self-test failed: {error}");
            std::process::exit(1);
        }
        eprintln!("Nebula desktop self-test passed");
        return;
    }
    nebula_ui_lib::run();
}
