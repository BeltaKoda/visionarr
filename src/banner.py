VISIONARR_ASCII = r"""
 __      ___     _                            
 \ \    / (_)   (_)                           
  \ \  / / _ ___ _  ___  _ __   __ _ _ __ _ __ 
   \ \/ / | / __| |/ _ \| '_ \ / _` | '__| '__|
    \  /  | \__ \ | (_) | | | | (_| | |  | |   
     \/   |_|___/_|\___/|_| |_|\__,_|_|  |_|   
                                              
       Dolby Vision Profile Converter
"""

VISIONARR_ASCII_SMALL = r"""
╦  ╦╦╔═╗╦╔═╗╔╗╔╔═╗╦═╗╦═╗
╚╗╔╝║╚═╗║║ ║║║║╠═╣╠╦╝╠╦╝
 ╚╝ ╩╚═╝╩╚═╝╝╚╝╩ ╩╩╚═╩╚═
"""

def print_banner(version: str = "1.0.0"):
    """Print the startup banner."""
    print(VISIONARR_ASCII)
    print(f"                    v{version}")
    print("          by BeltaKoda")
    print("   github.com/BeltaKoda/visionarr")
    print()
