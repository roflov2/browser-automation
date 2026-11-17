"""Entry point for every drip-pricing audit workflow.

Usage:
    python main.py coffee https://redbrick.coffee https://onacoffee.com.au --products 3
    python main.py virgin --origin BNE --destination PER --date 2027-02-14
    python main.py ubereats --address "1 Jeffcott Place, Canberra ACT 2615" --query "pizza" --items 2
    python main.py <workflow> --help
"""

from audit.cli import main

if __name__ == "__main__":
    main()
