def locate(size, key, read):
    for index in range(size):
        if read(index) == key:
            return index
    return -1
