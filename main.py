"""Entry point: walk any purchase or booking funnel to the payment wall, then stop.

Usage:
    python main.py --url https://shop.example.com.au
    python main.py --url https://shop.example.com.au --max-steps 20 --headless
"""

from checkout_navigator.run import main

if __name__ == "__main__":
    main()
