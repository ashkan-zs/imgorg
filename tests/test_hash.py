from organize_images import calculate_hash


def test_calculate_hash(tmp_path):
    file_path = tmp_path / "photo.jpg"
    file_path.write_bytes(b"hello world")

    result = calculate_hash(file_path)

    assert len(result) == 64