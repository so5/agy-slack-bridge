import os

import pytest

import scratch_clean as sc


@pytest.fixture
def root(tmp_path):
    (tmp_path / "scratch").mkdir()
    return tmp_path


def test_removes_files_and_directories_recursively(root):
    (root / "scratch/a.py").write_text("x")
    (root / "scratch/d/e").mkdir(parents=True)
    (root / "scratch/d/e/f.txt").write_text("x")
    done, errors = sc.clean(str(root), ["scratch/a.py", "scratch/d"])
    assert errors == [] and len(done) == 2
    assert list((root / "scratch").iterdir()) == []


def test_absolute_paths_work_and_a_child_of_a_removed_dir_is_skipped(root):
    (root / "scratch/d").mkdir()
    (root / "scratch/d/x").write_text("x")
    done, errors = sc.clean(str(root), [str(root / "scratch/d"), "scratch/d/x"])
    assert errors == [] and len(done) == 1


def test_all_or_nothing(root):
    (root / "scratch/a.py").write_text("x")
    (root / "keep.txt").write_text("x")
    done, errors = sc.clean(str(root), ["scratch/a.py", "keep.txt"])
    assert done == [] and errors
    assert (root / "scratch/a.py").exists() and (root / "keep.txt").exists()


@pytest.mark.parametrize("arg", ["scratch", "scratch/", "scratch/.", "scratch/..", "../x", "scratch/../keep.txt",
                                 "-rf", "/etc/hostname", "scratch/nothing"])
def test_rejects_what_is_outside_or_malformed(root, arg):
    (root / "keep.txt").write_text("x")
    done, errors = sc.clean(str(root), [arg])
    assert done == [] and errors and (root / "keep.txt").exists()


def test_a_symlink_in_scratch_is_removed_but_its_target_stays_wherever_it_is(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "precious").write_text("x")
    (root / "scratch/link").symlink_to(outside / "precious")
    (root / "scratch/dirlink").symlink_to(outside)
    done, errors = sc.clean(str(root), ["scratch/link", "scratch/dirlink"])
    assert errors == [] and len(done) == 2
    assert (outside / "precious").exists() and not os.path.lexists(root / "scratch/link")


def test_a_symlinked_directory_in_the_path_cannot_lead_outside(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "precious").write_text("x")
    (root / "scratch/out").symlink_to(outside)
    done, errors = sc.clean(str(root), ["scratch/out/precious"])
    assert done == [] and errors and (outside / "precious").exists()


def test_links_inside_a_removed_directory_are_not_followed(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "precious").write_text("x")
    (root / "scratch/d").mkdir()
    (root / "scratch/d/l").symlink_to(outside)
    done, errors = sc.clean(str(root), ["scratch/d"])
    assert errors == [] and (outside / "precious").exists() and not (root / "scratch/d").exists()


def test_main_exit_codes(root, capsys):
    (root / "scratch/a").write_text("x")
    assert sc.main(["--root", str(root), "scratch/a"]) == 0
    assert sc.main(["--root", str(root), "scratch/a"]) == 1          # already gone
    assert sc.main(["scratch/a"]) == 2                                # --root must come first
    assert sc.main(["--root", str(root)]) == 2
