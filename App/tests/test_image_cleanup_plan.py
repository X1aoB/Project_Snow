from scripts.plan_image_cleanup import select_candidates


def test_cleanup_only_selects_unreferenced_known_images():
    def image(name, repos):
        return {"Id": f"sha256:{name * 64}", "RepoDigests": repos, "Size": 100}

    snow = "ghcr.io/x1aob/project_snow-public"
    images = [
        image("a", [f"{snow}@sha256:{'a' * 64}"]),
        image("b", [f"{snow}@sha256:{'b' * 64}"]),
        image("c", [f"{snow}@sha256:{'c' * 64}"]),
        image("d", ["langgenius/dify-api@sha256:" + "d" * 64]),
        image("e", []),
        image("f", [f"{snow}@sha256:{'f' * 64}", "other/image:latest"]),
    ]
    result = select_candidates(images, {"sha256:" + "a" * 64}, {"sha256:" + "b" * 64})
    assert [row["id"] for row in result] == ["sha256:" + "c" * 64]
