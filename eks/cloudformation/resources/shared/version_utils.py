"""
Shared utility functions for version comparison across Lambda functions.
"""


def compare_versions(version1, version2):
    """
    Compare two semantic versions, handling EKS addon versions with suffixes like 'eksbuild'
    Returns: 1 if version1 > version2, -1 if version1 < version2, 0 if equal
    """
    def extract_numeric_parts(version):
        """Extract numeric parts from version string, handling suffixes"""
        # Remove 'v' prefix if present
        v = version.lstrip('v')
        
        # Split on hyphen first to separate main version from build suffixes
        main_version = v.split('-')[0]
        
        # Split by dots
        parts = main_version.split('.')
        numeric_parts = []
        
        for part in parts:
            # Extract only the numeric portion before any hyphen or non-numeric characters
            numeric_part = ''
            for char in part:
                if char.isdigit():
                    numeric_part += char
                else:
                    break
            
            # If we found numeric digits, convert to int, otherwise use 0
            if numeric_part:
                numeric_parts.append(int(numeric_part))
            else:
                numeric_parts.append(0)
        
        return numeric_parts
    
    try:
        parts1 = extract_numeric_parts(version1)
        parts2 = extract_numeric_parts(version2)
        
        # Pad shorter version with zeros
        max_len = max(len(parts1), len(parts2))
        parts1.extend([0] * (max_len - len(parts1)))
        parts2.extend([0] * (max_len - len(parts2)))
        
        # Compare each part
        for p1, p2 in zip(parts1, parts2):
            if p1 > p2:
                return 1
            elif p1 < p2:
                return -1
        
        return 0
        
    except Exception as e:
        print("Warning: Error comparing versions '{}' and '{}': {}".format(version1, version2, e))
        # If comparison fails, assume versions are equal to avoid update attempts
        return 0
