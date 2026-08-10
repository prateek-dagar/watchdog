import json

def main():
    try:
        with open("coverage.json") as f:
            data = json.load(f)
        pct = data["totals"]["percent_covered_display"]
        with open("coverage_percentage.txt", "w") as f:
            f.write(f"{pct}%")
    except Exception as e:
        print(f"Error extracting coverage: {e}")
        with open("coverage_percentage.txt", "w") as f:
            f.write("N/A")

if __name__ == "__main__":
    main()
