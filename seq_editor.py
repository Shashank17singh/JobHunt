import sys

def main():
    if len(sys.argv) < 2:
        sys.exit(1)
    file_path = sys.argv[1]
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    with open(file_path, 'w', encoding='utf-8') as f:
        for line in lines:
            if line.startswith('pick '):
                f.write(line.replace('pick ', 'edit ', 1))
            else:
                f.write(line)

if __name__ == '__main__':
    main()
