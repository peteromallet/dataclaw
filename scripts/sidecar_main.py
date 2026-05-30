import multiprocessing

from dataclaw.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main(api_key="your_valid_api_key_here")