import os
import json

def get_facing_from_aspect(aspect):
    ''' Convert an aspect value (0-360.0) to a direction, clockwise by ArcGIS definition. '''

    x = float(aspect)

    if (x > 360.0) or (x < 0): # Invalid
        return ""
    
    result = ""
    if (x > 337.5) or (x <= 22.5): result = "N"
    elif (x > 22.5) and (x <= 67.5): result = "NE"
    elif (x > 67.5) and (x <= 112.5): result = "E"
    elif (x > 112.5) and (x <= 157.5): result = "SE"
    elif (x > 157.5) and (x <= 202.5): result = "S"
    elif (x > 202.5) and (x <= 247.5): result = "SW"
    elif (x > 247.5) and (x <= 292.5): result = "W"
    elif (x > 292.5) and (x <= 337.5): result = "NW"

    return result


def match_aspect_altitude_to_forecast(forecasts, aspect, altitude):
    ''' Operate on one list of SAIS forecasts in the same day
        to see which risk altitude range does the altitude fit 
        in. '''
    
    # If forecasts not available.
    if len(forecasts) <= 0:
        return -1
    
    # Validate aspect.
    if not (0 <= aspect <= 360):
        return -1
    forecast_search = [i for i in forecasts if str(i[3]) == get_facing_from_aspect(aspect)]
    if len(forecast_search) < 1:
        return -1
    forecast = forecast_search[0]

    lower_boundary = int(forecast[4])
    middle_boundary = int(forecast[5])
    upper_boundary = int(forecast[6])
    lower_primary_colour = int(forecast[7])
    lower_secondary_colour = int(forecast[8])
    upper_primary_colour = int(forecast[9])
    upper_secondary_colour = int(forecast[10])

    if (altitude < lower_boundary):
        #Below snow line, no altitude-related risk.
        return 0
    elif (altitude >= lower_boundary) and (altitude < middle_boundary):
        return max(lower_primary_colour, lower_secondary_colour)
    elif (altitude >= middle_boundary) and (altitude <= upper_boundary):
        return max(upper_primary_colour, upper_secondary_colour)
    else:
        #Above snow line, no altitude-related risk.
        return 0


def risk_code_to_colour(risk_code):
    ''' Return an RGB 3-tuple for the colour represented by the risk_code. '''

    # [None: Gray, Low: Green, Moderate: Light Yellow, Considerable: Light Orange, High: Light Red, Very High: Dark Red]
    risks = [(192,192,192), (153,255,153), (255,255,153), (255,178,102), (255,102,102), (102,0,0)]
    
    if (risk_code < 0) or (risk_code) > 5: # Invalid data, not filling that pixel.
        return (255,255,255,0)
    else:
        # Add a 50% transparency channel (255/2)
        risks[risk_code] = risks[risk_code] + (127,)
        return risks[risk_code]