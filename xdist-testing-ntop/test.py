import pytest
import time
import pathlib

@pytest.mark.xdist_custom(name="low_4")
def test_writer():
    time.sleep(2)
    file_path = pathlib.Path("/home/chrisiaconetti/dev/test_ran.txt")
    with open(file_path, 'w') as writer:
        writer.write("The test ran!\n")
    assert True

@pytest.mark.xdist_custom(name="low_4")
def test_1():
    time.sleep(2)
    assert True

@pytest.mark.xdist_custom(name="low_4")
def test_2():
    time.sleep(2)
    assert True

@pytest.mark.xdist_custom(name="low_4")
def test_3():
    time.sleep(2)
    assert True

@pytest.mark.xdist_custom(name="low_4")
def test_4():
    time.sleep(2)
    assert True

@pytest.mark.xdist_custom(name="med_2")
def test_5():
    time.sleep(10)
    assert True

@pytest.mark.xdist_custom(name="med_2")
def test_6():
    time.sleep(10)
    assert True

# @pytest.mark.xdist_custom(name="med_2")
# def test_7():
#     time.sleep(10)
#     assert True

# @pytest.mark.xdist_custom(name="med_2")
# def test_8():
#     time.sleep(10)
#     assert True

# @pytest.mark.xdist_custom(name="high_1")
# def test_9():
#     time.sleep(30)
#     assert True

# @pytest.mark.xdist_custom(name="high_1")
# def test_10():
#     time.sleep(30)
#     assert True

# def test_11():
#     time.sleep(1)
#     assert True

# def test_12():
#     time.sleep(1)
#     assert True

# def test_13():
#     time.sleep(1)
#     assert True

# def test_14():
#     time.sleep(1)
#     assert True